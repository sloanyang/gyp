#!/usr/bin/python

import optparse
import os.path
import re
import sys


# TODO(mark): variables_hack is a temporary hack to work with conditional
# sections since real expression parsing is not currently available.
# Additional variables are added to this list when a generator is imported
# in main.
variables_hack = []


def BuildFileAndTarget(build_file, target):
  # NOTE: If you just want to split up target into a build_file and target,
  # and you know that target already has a build_file that's been produced by
  # this function, pass '' for build_file.

  target_split = target.split(':', 1)
  if len(target_split) == 2:
    [build_file_rel, target] = target_split

    # If a relative path, build_file_rel is relative to the directory
    # containing build_file.  If build_file is not in the current directory,
    # build_file_rel is not a usable path as-is.  Resolve it by interpreting it
    # as relative to build_file.  If build_file_rel is absolute, it is usable
    # as a path regardless of the current directory, and os.path.join will
    # return it as-is.
    build_file = os.path.normpath(os.path.join(os.path.dirname(build_file),
                                               build_file_rel))

  return [build_file, target, build_file + ':' + target]


def QualifiedTarget(build_file, target):
  # "Qualified" means the file that a target was defined in and the target
  # name, separated by a colon.
  return BuildFileAndTarget(build_file, target)[2]


def ExceptionAppend(e, msg):
  if not e.args:
    e.args = [msg]
  elif len(e.args) == 1:
    e.args = [str(e.args[0]) + ' ' + msg]
  else:
    e.args = [str(e.args[0]) + ' ' + msg, e.args[1:]]


def LoadOneBuildFile(build_file_path):
  build_file = open(build_file_path)
  build_file_contents = build_file.read()
  build_file.close()

  build_file_data = None
  try:
    build_file_data = eval(build_file_contents)
  except SyntaxError, e:
    e.filename = build_file_path
    raise
  except Exception, e:
    ExceptionAppend(e, 'while reading ' + build_file_path)
    raise

  # TODO(mark): Apply "pre"/"early" variable expansion here.

  # Apply "pre"/"early" conditionals.
  ProcessConditionalsInDict(build_file_data)

  # Scan for includes and merge them in.
  try:
    LoadBuildFileIncludesIntoDict(build_file_data, build_file_path)
  except Exception, e:
    ExceptionAppend(e, 'while reading includes of ' + build_file_path)
    raise

  return build_file_data


def LoadBuildFileIncludesIntoDict(subdict, subdict_path):
  if 'includes' in subdict:
    # Unhook the includes list, it's no longer needed.
    includes_list = subdict['includes']
    del subdict['includes']

    # Replace it by merging in the included files.
    for include in includes_list:
      MergeDicts(subdict, LoadOneBuildFile(include), subdict_path, include)

  # Recurse into subdictionaries.
  for k, v in subdict.iteritems():
    if v.__class__ == dict:
      LoadBuildFileIncludesIntoDict(v, subdict_path)
    elif v.__class__ == list:
      LoadBuildFileIncludesIntoList(v, subdict_path)


# This presently only recurses into lists so that it can look for dicts.
# Should it allow includes within lists inline?  TODO(mark): Decide.
#   sources: [
#     "source1.cc",
#     { "includes": [ "some_included_file" ] },
#     "source2.cc"
#   ]
def LoadBuildFileIncludesIntoList(sublist, sublist_path):
  for item in sublist:
    if item.__class__ == dict:
      LoadBuildFileIncludesIntoDict(item, sublist_path)
    elif item.__class__ == list:
      LoadBuildFileIncludesIntoList(item, sublist_path)


# TODO(mark): I don't love this name.  It just means that it's going to load
# a build file that contains targets and is expected to provide a targets dict
# that contains the targets...
def LoadTargetBuildFile(build_file_path, data={}):
  if build_file_path in data:
    # Already loaded.
    return

  build_file_data = LoadOneBuildFile(build_file_path)
  data[build_file_path] = build_file_data

  # ...it's loaded and it should have EARLY references and conditionals
  # all resolved and includes merged in...at least it will eventually...

  # Look for dependencies.  This means that dependency resolution occurs
  # after "pre" conditionals and variable expansion, but before "post" -
  # in other words, you can't put a "dependencies" section inside a "post"
  # conditional within a target.

  if 'targets' in build_file_data:
    for target_dict in build_file_data['targets']:
      if 'dependencies' not in target_dict:
        continue
      for dependency in target_dict['dependencies']:
        other_build_file = BuildFileAndTarget(build_file_path, dependency)[0]
        LoadTargetBuildFile(other_build_file, data)

  return data


# TODO(mark): This needs a way to choose which conditions dict to look at.
# Right now, it's called "conditions" but that's just so that I don't need
# to edit the names in the input files.  The existing conditions are all
# "early" or "pre" conditions.  Support for "late"/"post"/"target" conditions
# needs to be added as well.
def ProcessConditionalsInDict(subdict):
  if 'conditions' in subdict:
    # Unhook the conditions list, it's no longer needed.
    conditions_dict = subdict['conditions']
    del subdict['conditions']

    # Evaluate conditions and merge in the dictionaries for the ones that pass.
    for condition in conditions_dict:
      [expression, settings_dict] = condition
      # TODO(mark): This is ever-so-slightly better than it was initially when
      # 'OS==mac' was hard-coded, but expression evaluation is needed.
      if expression in variables_hack:
        # OK to pass '', '' for the build files because everything comes from
        # the same build file and everything is already relative to the same
        # place.
        MergeDicts(subdict, settings_dict, '', '')

  # Recurse into subdictionaries.
  for k, v in subdict.iteritems():
    if v.__class__ == dict:
      ProcessConditionalsInDict(v)
    elif v.__class__ == list:
      ProcessConditionalsInList(v)


# TODO(mark): The same comment about list recursion and whether to allow
# inlines in lists at LoadBuildFileIncludesIntoList applies to this function.
def ProcessConditionalsInList(sublist):
  for item in sublist:
    if item.__class__ == dict:
      ProcessConditionalsInDict(item)
    elif item.__class__ == list:
      ProcessConditionalsInList(item)


class DependencyTreeNode(object):
  """

  Attributes:
     ref: A reference to an object that this DependencyTreeNode represents.
     dependencies: List of DependencyTreeNodes on which this one depends.
     dependents: List of DependencyTreeNodes that depend on this one.
  """

  class CircularException(Exception):
    pass

  def __init__(self, ref):
    self.ref = ref
    self.dependencies = []
    self.dependents = []

  def FlattenToList(self):
    # flat_list is the sorted list of dependencies - actually, the list items
    # are the "ref" attributes of DependencyTreeNodes.  Every target will
    # appear in flat_list after all of its dependencies, and before all of its
    # dependents.
    flat_list = []

    # in_degree_zeros is the list of DependencyTreeNodes that have no
    # dependencies not in flat_list.  Initially, it is a copy of the children
    # of this node, because when the graph was built, nodes with no
    # dependencies were made implicit dependents of the root node.
    in_degree_zeros = self.dependents[:]

    while in_degree_zeros:
      # Nodes in in_degree_zeros have no dependencies not in flat_list, so they
      # can be appended to flat_list.  Take these nodes out of in_degree_zeros
      # as work progresses, so that the next node to process from the list can
      # always be accessed at a consistent position.
      node = in_degree_zeros[0]
      flat_list.append(node.ref)
      del in_degree_zeros[0]

      # Look at dependents of the node just added to flat_list.  Some of them
      # may now belong in in_degree_zeros.
      for node_dependent in node.dependents:
        is_in_degree_zero = True
        for node_dependent_dependency in node_dependent.dependencies:
          if not node_dependent_dependency.ref in flat_list:
            # The dependent one or more dependencies not in flat_list.  There
            # will be more chances to add it to flat_list when examining
            # it again as a dependent of those other dependencies, provided
            # that there are no cycles.
            is_in_degree_zero = False
            break

        if is_in_degree_zero:
          # All of the dependent's dependencies are already in flat_list.  Add
          # it to in_degree_zeros where it will be processed in a future
          # iteration of the outer loop.
          in_degree_zeros.append(node_dependent)

    return flat_list

  def DeepDependents(self, dependents=None):
    if dependents == None:
      dependents = []

    for dependent in self.dependents:
      if dependent.ref not in dependents:
        # Put each dependent as well as its dependents into the list.
        dependents.append(dependent.ref)
        dependent.DeepDependents(dependents)

    return dependents


def BuildDependencyList(targets):
  # Create a DependencyTreeNode for each target.  Put it into a dict for easy
  # access.
  dependency_nodes = {}
  for target, spec in targets.iteritems():
    if not target in dependency_nodes:
      dependency_nodes[target] = DependencyTreeNode(target)

  # Set up the dependency links.  Targets that have no dependencies are treated
  # as dependent on root_node.
  root_node = DependencyTreeNode(None)
  for target, spec in targets.iteritems():
    target_node = dependency_nodes[target]
    if not 'dependencies' in spec or len(spec['dependencies']) == 0:
      target_node.dependencies = [root_node]
      root_node.dependents.append(target_node)
    else:
      for index in range(0, len(spec['dependencies'])):
        dependency = spec['dependencies'][index]
        target_build_file = BuildFileAndTarget('', target)[0]
        dependency = QualifiedTarget(target_build_file, dependency)
        # Store the qualified name of the target even if it wasn't originally
        # qualified in the dict.  Others will find this useful as well.
        spec['dependencies'][index] = dependency
        dependency_node = dependency_nodes[dependency]
        target_node.dependencies.append(dependency_node)
        dependency_node.dependents.append(target_node)

  # Take the root node out of the list because it doesn't correspond to a real
  # target.
  flat_list = root_node.FlattenToList()

  # If there's anything left unvisited, there must be a circular dependency
  # (cycle).  If you need to figure out what's wrong, look for elements of
  # targets that are not in flat_list.
  if len(flat_list) != len(targets):
    raise DependencyTreeNode.CircularException, \
        'Some targets not reachable, cycle in dependency graph detected'

  return [dependency_nodes, flat_list]


def RelativePath(path, relative_to):
  # Assuming both |path| and |relative_to| are relative to the current
  # directory, returns a relative path that identifies path relative to
  # relative_to.

  if os.path.isabs(path) != os.path.isabs(relative_to):
    # If one of the paths is absolute, both need to be absolute.
    path = os.path.abspath(path)
    relative_to = os.path.abspath(relative_to)
  else:
    # If both paths are relative, make sure they're normalized.
    path = os.path.normpath(path)
    relative_to = os.path.normpath(relative_to)

  # Split the paths into components.  As a special case, if either path is
  # the current directory, use an empty list as a split-up path.  This must be
  # done because the code that follows is unprepared to deal with "." meaning
  # "current directory" and it will instead assume that it's a subdirectory,
  # which is wrong.  It's possible to wind up with "." when it's passed to this
  # function, for example, by taking the dirname of a relative path in the
  # current directory.
  if path == os.path.curdir:
    path_split = []
  else:
    path_split = path.split(os.path.sep)

  if relative_to == os.path.curdir:
    relative_to_split = []
  else:
    relative_to_split = relative_to.split(os.path.sep)

  # Determine how much of the prefix the two paths share.
  prefix_len = len(os.path.commonprefix([path_split, relative_to_split]))

  # Put enough ".." components to back up out of relative_to to the common
  # prefix, and then append the part of path_split after the common prefix.
  relative_split = [os.path.pardir] * (len(relative_to_split) - prefix_len) + \
                   path_split[prefix_len:]

  # Turn it back into a string and we're done.
  return os.path.join(*relative_split)


def MergeLists(to, fro, to_file, fro_file, is_paths=False):
  # TODO(mark): Support a way for the "fro" list to declare how it wants to
  # be merged into the "to" list.  Right now, "append" is always used, but
  # other possible policies include "prepend" and "replace".  Perhaps the
  # "fro" list can include a special first token, or perhaps the "fro" list
  # can have a sibling or something identifying the desired treatment.  Also,
  # "append" may not always be the most appropriate merge policy.  For
  # example, when merging file-wide .gyp settings into targets, it seems more
  # logical to prepend file-wide settings to target-specific ones, which are
  # thought of as "inheriting" file-wide setings.
  for item in fro:
    if isinstance(item, str) or isinstance(item, int):
      # The cheap and easy case.
      # TODO(mark): Expand variables here?  I waffle a bit more on this below,
      # in MergeDicts.
      if is_paths and to_file != fro_file:
        # If item is a relative path, it's relative to the build file dict that
        # it's coming from.  Fix it up to make it relative to the build file
        # dict that it's going into.
        # TODO(mark): We might want to exclude some things here even if
        # is_paths is true.
        path = os.path.normpath(os.path.join(
            RelativePath(os.path.dirname(fro_file), os.path.dirname(to_file)),
            item))
        to.append(path)
      else:
        to.append(item)
    elif isinstance(item, dict):
      # Insert a copy of the dictionary.
      to.append(item.copy())
    elif isinstance(item, list):
      # Insert a copy of the list.
      to.append(item[:])
    else:
      raise TypeError, \
          'Attempt to merge list item of unsupported type ' + \
          item.__class__.__name__


def MergeDicts(to, fro, to_file, fro_file):
  # I wanted to name the parameter "from" but it's a Python keyword...
  for k, v in fro.iteritems():
    # It would be nice to do "if not k in to: to[k] = v" but that wouldn't give
    # copy semantics.  Something else may want to merge from the |fro| dict
    # later, and having the same dict ref pointed to twice in the tree isn't
    # what anyone wants considering that the dicts may subsequently be
    # modified.
    if k in to and v.__class__ != to[k].__class__:
      raise TypeError, \
          'Attempt to merge dict value of type ' + v.__class__.__name__ + \
          ' into incompatible type ' + to[k].__class__.__name__ + \
          ' for key ' + k
    if isinstance(v, str) or isinstance(v, int):
      # Overwrite the existing value, if any.  Cheap and easy.
      # TODO(mark): Expand variables here?  We may want a way to make use
      # of the existing string value, if any, and variable expansion might
      # be the right solution.  On the other hand, it's possible that we
      # might want to do all expansions in a separate step completely
      # independent of merging.  These questions need answers.
      to[k] = v
    elif isinstance(v, dict):
      # Recurse, guaranteeing copies will be made of objects that require it.
      if not k in to:
        to[k] = {}
      MergeDicts(to[k], v, to_file, fro_file)
    elif isinstance(v, list):
      # Call MergeLists, which will make copies of objects that require it.
      if not k in to:
        to[k] = []
      is_paths = k in ['include_dirs', 'sources', 'xcode_framework_dirs']
      MergeLists(to[k], v, to_file, fro_file, is_paths)
    else:
      raise TypeError, \
          'Attempt to merge dict value of unsupported type ' + \
          v.__class__.__name__ + ' for key ' + k


def FindBuildFiles():
  extension = '.gyp'
  files = os.listdir(os.getcwd())
  build_files = []
  for file in files:
    if file[-len(extension):] == extension:
      build_files.append(file)
  return build_files


def main(args):
  my_name = os.path.basename(sys.argv[0])

  parser = optparse.OptionParser()
  usage = 'usage: %s [-f format] [build_file ...]'
  parser.set_usage(usage.replace('%s', '%prog'))
  parser.add_option('-f', '--format', dest='format',
                    help='Output format to generate')
  (options, build_files) = parser.parse_args(args)
  if not options.format:
    options.format = {'darwin': 'xcodeproj',
                      'win32':  'msvs',
                      'cygwin': 'msvs'}[sys.platform]
  if not build_files:
    build_files = FindBuildFiles()
  if not build_files:
    print >>sys.stderr, (usage + '\n\n%s: error: no build_file') % \
                        (my_name, my_name)
    return 1

  generator_name = 'gyp.generator.' + options.format
  # These parameters are passed in order (as opposed to by key)
  # because ActivePython cannot handle key parameters to __import__.
  generator = __import__(generator_name, globals(), locals(), generator_name)
  variables_hack.extend(generator.variables_hack)

  # Load build files.  This loads every target-containing build file into
  # the |data| dictionary such that the keys to |data| are build file names,
  # and the values are the entire build file contents after "early" or "pre"
  # processing has been done and includes have been resolved.
  data = {}
  for build_file in build_files:
    LoadTargetBuildFile(build_file, data)

  # Build a dict to access each target's subdict by qualified name.
  targets = {}
  for build_file in data:
    if 'targets' in data[build_file]:
      for target in data[build_file]['targets']:
        target_name = QualifiedTarget(build_file, target['name'])
        targets[target_name] = target

  # BuildDependencyList will also fix up all dependency lists to contain only
  # qualified names.  That makes it much easier to see if a target is already
  # in a dependency list, because the name it will be listed by is known.
  # This is used below when the dependency lists are adjusted for static
  # libraries.  The only thing I don't like about this is that it seems like
  # BuildDependencyList shouldn't modify "targets".  I thought we looped over
  # "targets" too many times, though, and that seemed like a good place to do
  # this fix-up.  We may want to revisit where this is done.
  [dependency_nodes, flat_list] = BuildDependencyList(targets)

  # TODO(mark): Make all of this stuff generic.  WORK IN PROGRESS.  It's a
  # lot cleaner than it used to be, but there's still progress to be made.
  # The whole file above this point is in pretty good shape, everything
  # below this line is kind of a wasteland.

  # Look at each project's settings dict, and merge settings into targets.
  # TODO(mark): Figure out when we should do this step.  Seems like it should
  # happen earlier.  Also, the policy here should be for dict keys in the base
  # settings dict to NOT overwrite keys in the target, and for list items in
  # the base settings dict to be PREPENDED to target lists instead of
  # appended.
  for build_file_name, build_file_data in data.iteritems():
    if 'settings' in build_file_data:
      file_settings = build_file_data['settings']
      for target_dict in build_file_data['targets']:
        MergeDicts(target_dict, file_settings, build_file_name, build_file_name)

  # Now look for dependent_settings sections in dependencies, and merge
  # settings.
  for target in flat_list:
    target_dict = targets[target]
    if not 'dependencies' in target_dict:
      continue

    build_file = BuildFileAndTarget('', target)[0]
    for dependency in target_dict['dependencies']:
      # The name is already relative, so use ''.
      [dep_build_file, dep_target_unq, dep_target_q] = \
          BuildFileAndTarget('', dependency)
      dependency_dict = targets[dep_target_q]
      if not 'dependent_settings' in dependency_dict:
        continue

      dependent_settings = dependency_dict['dependent_settings']
      MergeDicts(target_dict, dependent_settings, build_file, dep_build_file)

  # TODO(mark): This logic is rough, but it works for base_unittests.
  # Set up computed dependencies.  For each non-static library target, look
  # at the entire dependency hierarchy and add any static libraries as computed
  # dependencies.  Static library targets have no computed dependencies.
  for target in flat_list:
    target_dict = targets[target]

    # If we've got a static library here...
    if target_dict['type'] == 'static_library':
      dependents = dependency_nodes[target].DeepDependents()
      # TODO(mark): Probably want dependents to be sorted in the order that
      # they appear in flat_list.

      # Look at every target that depends on it, even indirectly...
      for dependent in dependents:
        [dependent_bf, dependent_unq, dependent_q] = \
            BuildFileAndTarget('', dependent)
        dependent_dict = targets[dependent_q]

        # If the dependent isn't a static library...
        if dependent_dict['type'] != 'static_library':

          # Make it depend on the static library if it doesn't already...
          if not 'dependencies' in dependent_dict:
            dependent_dict['dependencies'] = []
          if not target in dependent_dict['dependencies']:
            dependent_dict['dependencies'].append(target)

          # ...and make it link against the libraries that the static library
          # wants, if it doesn't already...
          if 'libraries' in target_dict:
            if not 'libraries' in dependent_dict:
              dependent_dict['libraries'] = []
            for library in target_dict['libraries']:
              if not library in dependent_dict['libraries']:
                dependent_dict['libraries'].append(library)

      # The static library doesn't need its dependencies or libraries any more.
      if 'dependencies' in target_dict:
        del target_dict['dependencies']
      if 'libraries' in target_dict:
        del target_dict['libraries']

  # Do source_patterns.
  # TODO(mark): This needs to be refactored real soon now.
  # I'm positive I wrote this comment before, but now it's gone.  Here's the
  # deal: this should be made more general, so that we can have arbitrary
  # *_patterns sections.  That probably means that it should be called
  # sources_patterns instead of source_patterns.  While we're thinking about
  # renaming it, maybe _patterns isn't the best name anyway.  Apparently I
  # called it source_rules in a meeting yesterday.  Also, the only action
  # supported here is "exclude" which seems to imply that there should be an
  # "include" but I'm having a hard time coming up with a good case for
  # that.  Someone suggested looking at the filesystem (which implies a glob
  # for include patterns rather than a RE).  I suppose we could do that, but
  # I don't really love it.
  for target in flat_list:
    [build_file, target_unq] = BuildFileAndTarget('', target)[0:2]
    target_dict = targets[target]

    # Key names are subject to change!
    if 'source_excludes' in target_dict:
      for source_exclude in target_dict['source_excludes']:
        if source_exclude in target_dict['sources']:
          target_dict['sources'].remove(source_exclude)

    if 'source_patterns' in target_dict:
      for source_pattern in target_dict['source_patterns']:
        [action, pattern] = source_pattern
        pattern_re = re.compile(pattern)
        # Ugh, need to make a copy up front because we can't modify the list
        # while iterating through it.  This may need some rethinking.  That
        # makes it TODO(mark).
        new_sources = target_dict['sources'][:]
        for source in target_dict['sources']:
          if pattern_re.search(source):
            if action == 'exclude':
              new_sources.remove(source)
        target_dict['sources'] = new_sources

  # TODO(mark): Pass |data| for now because the generator needs a list of
  # build files that came in.  In the future, maybe it should just accept
  # a list, and not the whole data dict.
  # NOTE: flat_list is the flattened dependency graph specifying the order
  # that targets may be built.  Build systems that operate serially or that
  # need to have dependencies defined before dependents reference them should
  # generate targets in the order specified in flat_list.
  generator.GenerateOutput(flat_list, targets, data)
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))