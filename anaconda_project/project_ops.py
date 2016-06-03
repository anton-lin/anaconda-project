# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Copyright © 2016, Continuum Analytics, Inc. All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
# ----------------------------------------------------------------------------
"""High-level operations on a project."""
from __future__ import absolute_import

import codecs
import os
import shutil
import tempfile

from anaconda_project.project import Project, ALL_COMMAND_TYPES
from anaconda_project import prepare
from anaconda_project import bundler
from anaconda_project import client
from anaconda_project.local_state_file import LocalStateFile
from anaconda_project.plugins.requirement import EnvVarRequirement
from anaconda_project.plugins.requirements.conda_env import CondaEnvRequirement
from anaconda_project.plugins.requirements.download import DownloadRequirement
from anaconda_project.plugins.requirements.download import _hash_algorithms
from anaconda_project.plugins.requirements.service import ServiceRequirement
from anaconda_project.plugins.providers.conda_env import _remove_env_path
from anaconda_project.internal.simple_status import SimpleStatus
import anaconda_project.conda_manager as conda_manager
from anaconda_project.internal.conda_api import parse_spec

_default_projectignore = """
# project-local contains your personal configuration choices and state
/project-local.yml

# Files autocreated by Python
__pycache__/
*.pyc
*.pyo
*.pyd

# Notebook stuff
/.ipynb_checkpoints

# Spyder stuff
/.spyderproject
""".lstrip()


def _add_projectignore_if_none(project_directory):
    filename = os.path.join(project_directory, ".projectignore")
    if not os.path.exists(filename):
        try:
            with codecs.open(filename, 'w', 'utf-8') as f:
                f.write(_default_projectignore)
        except IOError:
            pass


def create(directory_path, make_directory=False, name=None, icon=None, description=None):
    """Create a project skeleton in the given directory.

    Returns a Project instance even if creation fails or the directory
    doesn't exist, but in those cases the ``problems`` attribute
    of the Project will describe the problem.

    If the project.yml already exists, this simply loads it.

    This will not prepare the project (create environments, etc.),
    use the separate prepare calls if you want to do that.

    Args:
        directory_path (str): directory to contain project.yml
        make_directory (bool): True to create the directory if it doesn't exist
        name (str): Name of the new project or None to leave unset (uses directory name)
        icon (str): Icon for the new project or None to leave unset (uses no icon)
        description (str): Description for the new project or None to leave unset

    Returns:
        a Project instance
    """
    if make_directory and not os.path.exists(directory_path):
        try:
            os.makedirs(directory_path)
        except (IOError, OSError):  # py3=IOError, py2=OSError
            # allow project.problems to report the issue
            pass

    # do this first so Project constructor can load it
    _add_projectignore_if_none(directory_path)

    project = Project(directory_path)

    if name is not None:
        project.project_file.set_value('name', name)
    if icon is not None:
        project.project_file.set_value('icon', icon)
    if description is not None:
        project.project_file.set_value('description', description)

    # write out the project.yml; note that this will try to create
    # the directory which we may not want... so only do it if
    # we're problem-free.
    project.project_file.use_changes_without_saving()
    if len(project.problems) == 0:
        project.project_file.save()

    return project


def set_properties(project, name=None, icon=None, description=None):
    """Set simple properties on a project.

    This doesn't support properties which require prepare()
    actions to check their effects; see other calls such as
    ``add_dependencies()`` for those.

    This will fail if project.problems is non-empty.

    Args:
        project (``Project``): the project instance
        name (str): Name of the project or None to leave unmodified
        icon (str): Icon for the project or None to leave unmodified
        description (str): description for the project or None to leave unmodified

    Returns:
        a ``Status`` instance indicating success or failure
    """
    failed = project.problems_status()
    if failed is not None:
        return failed

    if name is not None:
        project.project_file.set_value('name', name)

    if icon is not None:
        project.project_file.set_value('icon', icon)

    if description is not None:
        project.project_file.set_value('description', description)

    project.project_file.use_changes_without_saving()

    if len(project.problems) == 0:
        # write out the project.yml if it looks like we're safe.
        project.project_file.save()
        return SimpleStatus(success=True, description="Project properties updated.")
    else:
        # revert to previous state (after extracting project.problems)
        status = SimpleStatus(success=False,
                              description="Failed to set project properties.",
                              errors=list(project.problems))
        project.project_file.load()
        return status


def _commit_requirement_if_it_works(project, env_var_or_class, env_spec_name=None):
    project.project_file.use_changes_without_saving()

    # See if we can perform the download
    result = prepare.prepare_without_interaction(project,
                                                 provide_whitelist=(env_var_or_class, ),
                                                 env_spec_name=env_spec_name)

    status = result.status_for(env_var_or_class)
    if status is None or not status:
        # reload from disk, discarding our changes because they did not work
        project.project_file.load()
    else:
        # yay!
        project.project_file.save()
    return status


def add_download(project, env_var, url, filename=None, hash_algorithm=None, hash_value=None):
    """Attempt to download the URL; if successful, add it as a download to the project.

    The returned ``Status`` should be a ``RequirementStatus`` for
    the download requirement if it evaluates to True (on success),
    but may be another subtype of ``Status`` on failure. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        env_var (str): env var to store the local filename
        url (str): url to download
        filename (optional, str): Name to give file or directory after downloading
        hash_algorithm (optional, str): Name of the algorithm to use for checksum verification
                                       must be present if hash_value is entered
        hash_value (optional, str): Checksum value to use for verification
                                       must be present if hash_algorithm is entered
    Returns:
        ``Status`` instance
    """
    assert ((hash_algorithm and hash_value) or (hash_algorithm is None and hash_value is None))
    failed = project.problems_status()
    if failed is not None:
        return failed
    requirement = project.project_file.get_value(['downloads', env_var])
    if requirement is None or not isinstance(requirement, dict):
        requirement = {}
        project.project_file.set_value(['downloads', env_var], requirement)

    requirement['url'] = url
    if filename:
        requirement['filename'] = filename

    if hash_algorithm:
        for _hash in _hash_algorithms:
            requirement.pop(_hash, None)
        requirement[hash_algorithm] = hash_value

    return _commit_requirement_if_it_works(project, env_var)


def remove_download(project, prepare_result, env_var):
    """Remove file or directory referenced by ``env_var`` from file system and the project.

    The returned ``Status`` will be an instance of ``SimpleStatus``. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        prepare_result (PrepareResult): result of a previous prepare
        env_var (str): env var to store the local filename

    Returns:
        ``Status`` instance
    """
    failed = project.problems_status()
    if failed is not None:
        return failed
    # Modify the project file _in memory only_, do not save
    requirement = project.find_requirements(env_var, klass=DownloadRequirement)
    if not requirement:
        return SimpleStatus(success=False, description="Download requirement: {} not found.".format(env_var))
    assert len(requirement) == 1  # duplicate env vars aren't allowed
    requirement = requirement[0]

    status = prepare.unprepare(project, prepare_result, whitelist=[env_var])
    if status:
        project.project_file.unset_value(['downloads', env_var])
        project.project_file.use_changes_without_saving()
        assert project.problems == []
        project.project_file.save()

    return status


# there are lots of builtin ways to do this but they wouldn't keep
# comments properly in ruamel.yaml's CommentedSeq. We don't want to
# copy or wholesale replace "items"
def _filter_inplace(predicate, items):
    i = 0
    while i < len(items):
        if predicate(items[i]):
            i += 1
        else:
            del items[i]


def _map_inplace(f, items):
    i = 0
    while i < len(items):
        items[i] = f(items[i])
        i += 1


def _update_env_spec(project, name, packages, channels, create):
    failed = project.problems_status()
    if failed is not None:
        return failed

    if packages is None:
        packages = []
    if channels is None:
        channels = []

    if not create and (name is not None):
        if name not in project.env_specs:
            problem = "Environment spec {} doesn't exist.".format(name)
            return SimpleStatus(success=False, description=problem)

    if name is None:
        env_dict = project.project_file.root
    else:
        env_dict = project.project_file.get_value(['environments', name])
        if env_dict is None:
            env_dict = dict()
            project.project_file.set_value(['environments', name], env_dict)

    # dependencies may be a "CommentedSeq" and we don't want to lose the comments,
    # so don't convert this thing to a regular list.
    dependencies = env_dict.get('dependencies', [])
    old_dependencies_set = set(parse_spec(dep).name for dep in dependencies)
    bad_specs = []
    updated_specs = []
    new_specs = []
    for dep in packages:
        if dep in dependencies:
            # no-op adding the EXACT same thing (don't move it around)
            continue
        parsed = parse_spec(dep)
        if parsed is None:
            bad_specs.append(dep)
        else:
            if parsed.name in old_dependencies_set:
                updated_specs.append((parsed.name, dep))
            else:
                new_specs.append(dep)

    if len(bad_specs) > 0:
        bad_specs_string = ", ".join(bad_specs)
        return SimpleStatus(success=False,
                            description="Could not add packages.",
                            errors=[("Bad package specifications: %s." % bad_specs_string)])

    # remove everything that we are changing the spec for
    def replace_spec(old):
        name = parse_spec(old).name
        for (replaced_name, new_spec) in updated_specs:
            if replaced_name == name:
                return new_spec
        return old

    _map_inplace(replace_spec, dependencies)
    # add all the new ones
    for added in new_specs:
        dependencies.append(added)

    env_dict['dependencies'] = dependencies

    # channels may be a "CommentedSeq" and we don't want to lose the comments,
    # so don't convert this thing to a regular list.
    new_channels = env_dict.get('channels', [])
    old_channels_set = set(new_channels)
    for channel in channels:
        if channel not in old_channels_set:
            new_channels.append(channel)
    env_dict['channels'] = new_channels

    status = _commit_requirement_if_it_works(project, CondaEnvRequirement, env_spec_name=name)

    return status


def add_env_spec(project, name, packages, channels):
    """Attempt to create the environment spec and add it to project.yml.

    The returned ``Status`` should be a ``RequirementStatus`` for
    the environment requirement if it evaluates to True (on success),
    but may be another subtype of ``Status`` on failure. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        name (str): environment spec name
        packages (list of str): dependencies (with optional version info, as for conda install)
        channels (list of str): channels (as they should be passed to conda --channel)

    Returns:
        ``Status`` instance
    """
    assert name is not None
    name = name.strip()
    return _update_env_spec(project, name, packages, channels, create=True)


def remove_env_spec(project, name):
    """Remove the environment spec from project directory and remove from project.yml.

    Returns a ``Status`` subtype (it won't be a
    ``RequirementStatus`` as with some other functions, just a
    plain status).

    Args:
        project (Project): the project
        name (str): environment spec name

    Returns:
        ``Status`` instance
    """
    assert name is not None
    if name == 'default':
        return SimpleStatus(success=False, description="Cannot remove default environment spec.")

    failed = project.problems_status()
    if failed is not None:
        return failed

    if name not in project.env_specs:
        problem = "Environment spec {} doesn't exist.".format(name)
        return SimpleStatus(success=False, description=problem)

    env_path = project.env_specs[name].path(project.directory_path)

    # For remove_service and remove_download, we use unprepare()
    # to do the cleanup; for the environment, it's awkward to do
    # that because the env we want to remove may not be the one
    # that was prepared. So instead we share some code with the
    # CondaEnvProvider but don't try to go through the unprepare
    # machinery.
    status = _remove_env_path(env_path)
    if status:
        project.project_file.unset_value(['environments', name])
        project.project_file.use_changes_without_saving()
        assert project.problems == []
        project.project_file.save()

    return status


def add_dependencies(project, env_spec_name, packages, channels):
    """Attempt to install dependencies then add them to project.yml.

    If the env_spec_name is None rather than an env name,
    dependencies are added in the global dependencies section (to
    all environment specs).

    The returned ``Status`` should be a ``RequirementStatus`` for
    the environment requirement if it evaluates to True (on success),
    but may be another subtype of ``Status`` on failure. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        env_spec_name (str): environment spec name or None for all environment specs
        packages (list of str): dependencies (with optional version info, as for conda install)
        channels (list of str): channels (as they should be passed to conda --channel)

    Returns:
        ``Status`` instance
    """
    return _update_env_spec(project, env_spec_name, packages, channels, create=False)


def remove_dependencies(project, env_spec_name, packages):
    """Attempt to remove dependencies from an environment in project.yml.

    If the env_spec_name is None rather than an env name,
    dependencies are removed from the global dependencies section
    (from all environments).

    The returned ``Status`` should be a ``RequirementStatus`` for
    the environment requirement if it evaluates to True (on success),
    but may be another subtype of ``Status`` on failure. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        env_spec_name (str): environment spec name or None for all environment specs
        packages (list of str): dependencies

    Returns:
        ``Status`` instance
    """
    # This is sort of one big ugly. What we SHOULD be able to do
    # is simply remove the dependency from project.yml then re-run
    # prepare, and if the packages aren't pulled in as deps of
    # something else, they get removed. This would work if our
    # approach was to always force the env to exactly the env
    # we'd have created from scratch, given our env config.
    # But that isn't our approach right now.
    #
    # So what we do right now is remove the package from the env,
    # and then remove it from project.yml, and then see if we can
    # still prepare the project.

    failed = project.problems_status()
    if failed is not None:
        return failed

    assert packages is not None
    assert len(packages) > 0

    if env_spec_name is None:
        envs = project.env_specs.values()
        unaffected_envs = []
    else:
        env = project.env_specs.get(env_spec_name, None)
        if env is None:
            problem = "Environment spec {} doesn't exist.".format(env_spec_name)
            return SimpleStatus(success=False, description=problem)
        else:
            envs = [env]
            unaffected_envs = list(project.env_specs.values())
            unaffected_envs.remove(env)
            assert len(unaffected_envs) == (len(project.env_specs) - 1)

    assert len(envs) > 0

    conda = conda_manager.new_conda_manager()

    for env in envs:
        prefix = env.path(project.directory_path)
        try:
            if os.path.isdir(prefix):
                conda.remove_packages(prefix, packages)
        except conda_manager.CondaManagerError:
            pass  # ignore errors; not all the envs will exist or have the package installed perhaps

    def envs_to_their_dicts(envs):
        env_dicts = []
        for env in envs:
            env_dict = project.project_file.get_value(['environments', env.name])
            if env_dict is not None:  # it can be None for the default environment (which doesn't have to be listed)
                env_dicts.append(env_dict)
        return env_dicts

    env_dicts = envs_to_their_dicts(envs)
    env_dicts.append(project.project_file.root)

    unaffected_env_dicts = envs_to_their_dicts(unaffected_envs)

    assert len(env_dicts) > 0

    previous_global_deps = set(project.project_file.root.get('dependencies', []))

    for env_dict in env_dicts:
        # dependencies may be a "CommentedSeq" and we don't want to lose the comments,
        # so don't convert this thing to a regular list.
        dependencies = env_dict.get('dependencies', [])
        removed_set = set(packages)
        _filter_inplace(lambda dep: dep not in removed_set, dependencies)
        env_dict['dependencies'] = dependencies

    # if we removed any deps from global, add them to the
    # individual envs that were not supposed to be affected.
    new_global_deps = set(project.project_file.root.get('dependencies', []))
    removed_from_global = (previous_global_deps - new_global_deps)
    for env_dict in unaffected_env_dicts:
        # dependencies may be a "CommentedSeq" and we don't want to lose the comments,
        # so don't convert this thing to a regular list.
        dependencies = env_dict.get('dependencies', [])
        dependencies.extend(list(removed_from_global))
        env_dict['dependencies'] = dependencies

    status = _commit_requirement_if_it_works(project, CondaEnvRequirement, env_spec_name=env_spec_name)

    return status


def add_variables(project, vars_to_add):
    """Add variables in project.yml and set their values in local project state.

    Returns a ``Status`` instance which evaluates to True on
    success and has an ``errors`` property (with a list of error
    strings) on failure.

    Args:
        project (Project): the project
        vars_to_add (list of tuple): key-value pairs

    Returns:
        ``Status`` instance
    """
    failed = project.problems_status()
    if failed is not None:
        return failed

    local_state = LocalStateFile.load_for_directory(project.directory_path)
    present_vars = {req.env_var for req in project.requirements if isinstance(req, EnvVarRequirement)}
    for varname, value in vars_to_add:
        local_state.set_value(['variables', varname], value)
        if varname not in present_vars:
            project.project_file.set_value(['variables', varname], None)
    project.project_file.save()
    local_state.save()

    return SimpleStatus(success=True, description="Variables added to the project file.")


def remove_variables(project, vars_to_remove):
    """Remove variables from project.yml and unset their values in local project state.

    Returns a ``Status`` instance which evaluates to True on
    success and has an ``errors`` property (with a list of error
    strings) on failure.

    Args:
        project (Project): the project
        vars_to_remove (list of tuple): key-value pairs

    Returns:
        ``Status`` instance
    """
    failed = project.problems_status()
    if failed is not None:
        return failed

    local_state = LocalStateFile.load_for_directory(project.directory_path)
    for varname in vars_to_remove:
        local_state.unset_value(['variables', varname])
        project.project_file.unset_value(['variables', varname])
    project.project_file.save()
    local_state.save()

    return SimpleStatus(success=True, description="Variables removed from the project file.")


def add_command(project, name, command_type, command):
    """Add a command to project.yml.

    Returns a ``Status`` subtype (it won't be a
    ``RequirementStatus`` as with some other functions, just a
    plain status).

    Args:
       project (Project): the project
       name (str): name of the command
       command_type (str): choice of `bokeh_app`, `notebook`, `unix` or `windows` command
       command (str): the command line or filename itself

    Returns:
       a ``Status`` instance
    """
    if command_type not in ALL_COMMAND_TYPES:
        raise ValueError("Invalid command type " + command_type + " choose from " + repr(ALL_COMMAND_TYPES))

    name = name.strip()

    failed = project.problems_status()
    if failed is not None:
        return failed

    command_dict = project.project_file.get_value(['commands', name])
    if command_dict is None:
        command_dict = dict()
        project.project_file.set_value(['commands', name], command_dict)

    command_dict[command_type] = command

    project.project_file.use_changes_without_saving()

    failed = project.problems_status(description="Unable to add the command.")
    if failed is not None:
        # reset, maybe someone added conflicting command line types or something
        project.project_file.load()
        return failed
    else:
        project.project_file.save()
        return SimpleStatus(success=True, description="Command added to project file.")


def update_command(project, name, command_type=None, command=None, new_name=None):
    """Update attributes of a command in project.yml.

    Returns a ``Status`` subtype (it won't be a
    ``RequirementStatus`` as with some other functions, just a
    plain status).

    Args:
       project (Project): the project
       name (str): name of the command
       command_type (str or None): choice of `bokeh_app`, `notebook`, `unix` or `windows` command
       command (str or None): the command line or filename itself; command_type must also be specified

    Returns:
       a ``Status`` instance
    """
    # right now update_command can be called "pointlessly" (with
    # no new command), this is because in theory it might let you
    # update other properties too, when/if commands have more
    # properties.
    if command_type is None and new_name is None:
        return SimpleStatus(success=True, description=("Nothing to change about command %s" % name))

    if command_type not in (list(ALL_COMMAND_TYPES) + [None]):
        raise ValueError("Invalid command type " + command_type + " choose from " + repr(ALL_COMMAND_TYPES))

    if command is None and command_type is not None:
        raise ValueError("If specifying the command_type, must also specify the command")

    failed = project.problems_status()
    if failed is not None:
        return failed

    if name not in project.commands:
        return SimpleStatus(success=False,
                            description="Failed to update command.",
                            errors=[("No command '%s' found." % name)])

    command_object = project.commands[name]
    if command_object.auto_generated:
        return SimpleStatus(success=False,
                            description="Failed to update command.",
                            errors=[("Autogenerated command '%s' can't be modified." % name)])

    command_dict = project.project_file.get_value(['commands', name])
    assert command_dict is not None

    if new_name:
        project.project_file.unset_value(['commands', name])
        project.project_file.set_value(['commands', new_name], command_dict)

    existing_types = set(command_dict.keys())
    conflicting_types = existing_types - set([command_type])
    # 'unix' and 'windows' don't conflict with one another
    if command_type == 'unix':
        conflicting_types = conflicting_types - set(['windows'])
    elif command_type == 'windows':
        conflicting_types = conflicting_types - set(['unix'])

    if command_type is not None:
        for conflicting in conflicting_types:
            del command_dict[conflicting]

        command_dict[command_type] = command

    project.project_file.use_changes_without_saving()

    failed = project.problems_status(description="Unable to add the command.")
    if failed is not None:
        # reset, maybe someone added a nonexistent bokeh app or something
        project.project_file.load()
        return failed
    else:
        project.project_file.save()
        return SimpleStatus(success=True, description="Command updated in project file.")


def remove_command(project, name):
    """Remove a command from project.yml.

    Returns a ``Status`` subtype (it won't be a
    ``RequirementStatus`` as with some other functions, just a
    plain status).

    Args:
       project (Project): the project
       name (string): name of the command to be removed

    Returns:
       a ``Status`` instance
    """
    failed = project.problems_status()
    if failed is not None:
        return failed

    if name not in project.commands:
        return SimpleStatus(success=False, description="Command: '{}' not found in project file.".format(name))

    command = project.commands[name]
    if command.auto_generated:
        return SimpleStatus(success=False, description="Cannot remove auto-generated command: '{}'.".format(name))

    project.project_file.unset_value(['commands', name])
    project.project_file.use_changes_without_saving()
    assert project.problems == []
    project.project_file.save()

    return SimpleStatus(success=True, description="Command: '{}' removed from project file.".format(name))


def add_service(project, service_type, variable_name=None):
    """Add a service to project.yml.

    The returned ``Status`` should be a ``RequirementStatus`` for
    the service requirement if it evaluates to True (on success),
    but may be another subtype of ``Status`` on failure. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        service_type (str): which kind of service
        variable_name (str): environment variable name (None for default)

    Returns:
        ``Status`` instance
    """
    failed = project.problems_status()
    if failed is not None:
        return failed

    known_types = project.plugin_registry.list_service_types()
    found = None
    for known in known_types:
        if known.name == service_type:
            found = known
            break

    if found is None:
        return SimpleStatus(success=False,
                            description="Unable to add service.",
                            logs=[],
                            errors=["Unknown service type '%s', we know about: %s" % (service_type, ", ".join(map(
                                lambda s: s.name, known_types)))])

    if variable_name is None:
        variable_name = found.default_variable

    assert len(known_types) == 1  # when this fails, see change needed in the loop below

    requirement_already_exists = False
    existing_requirements = project.find_requirements(env_var=variable_name)
    if len(existing_requirements) > 0:
        requirement = existing_requirements[0]
        if isinstance(requirement, ServiceRequirement):
            assert requirement.service_type == service_type
            # when the above assertion fails, add the second known type besides
            # redis in test_project_ops.py::test_add_service_already_exists_with_different_type
            # and then uncomment the below code.
            # if requirement.service_type != service_type:
            #    return SimpleStatus(success=False, description="Unable to add service.", logs=[],
            #                            errors=["Service %s already exists but with type '%s'" %
            #                              (variable_name, requirement.service_type)])
            # else:
            requirement_already_exists = True
        else:
            return SimpleStatus(success=False,
                                description="Unable to add service.",
                                logs=[],
                                errors=["Variable %s is already in use." % variable_name])

    if not requirement_already_exists:
        project.project_file.set_value(['services', variable_name], service_type)

    return _commit_requirement_if_it_works(project, variable_name)


def remove_service(project, prepare_result, variable_name):
    """Remove a service to project.yml.

    Returns a ``Status`` instance which evaluates to True on
    success and has an ``errors`` property (with a list of error
    strings) on failure.

    Args:
        project (Project): the project
        prepare_result (PrepareResult): result of a previous prepare
        variable_name (str): environment variable name for the service requirement

    Returns:
        ``Status`` instance
    """
    failed = project.problems_status()
    if failed is not None:
        return failed

    requirements = [req
                    for req in project.find_requirements(klass=ServiceRequirement)
                    if req.service_type == variable_name or req.env_var == variable_name]
    if not requirements:
        return SimpleStatus(success=False,
                            description="Service '{}' not found in the project file.".format(variable_name))

    if len(requirements) > 1:
        return SimpleStatus(success=False,
                            description=("Conflicting results, found {} matches, use list-services"
                                         " to identify which service you want to remove").format(len(requirements)))

    env_var = requirements[0].env_var

    status = prepare.unprepare(project, prepare_result, whitelist=[env_var])
    if not status:
        return status

    project.project_file.unset_value(['services', env_var])
    project.project_file.use_changes_without_saving()
    assert project.problems == []

    project.project_file.save()
    return SimpleStatus(success=True, description="Removed service '{}' from the project file.".format(variable_name))


def clean(project, prepare_result):
    """Blow away auto-provided state for the project.

    This should not remove any potential "user data" such as
    project-local.yml.

    This includes a call to ``anaconda_project.prepare.unprepare``
    but also removes the entire services/ and envs/ directories
    even if they contain leftovers that we didn't prepare in the
    most recent prepare() call.

    Args:
        project (Project): the project instance
        prepare_result (PrepareResult): result of a previous prepare

    Returns:
        a ``Status`` instance

    """
    status = prepare.unprepare(project, prepare_result)
    logs = status.logs
    errors = status.errors
    if status:
        logs = logs + [status.status_description]
    else:
        errors = errors + [status.status_description]

    # we also nuke any "debris" from non-current choices, like old
    # environments or services
    def cleanup_dir(dirname):
        if os.path.isdir(dirname):
            logs.append("Removing %s." % dirname)
            try:
                shutil.rmtree(dirname)
            except Exception as e:
                errors.append("Error removing %s: %s." % (dirname, str(e)))

    cleanup_dir(os.path.join(project.directory_path, "services"))
    cleanup_dir(os.path.join(project.directory_path, "envs"))

    if status and len(errors) == 0:
        return SimpleStatus(success=True, description="Cleaned.", logs=logs, errors=errors)
    else:
        return SimpleStatus(success=False, description="Failed to clean everything up.", logs=logs, errors=errors)


def bundle(project, filename):
    """Make an archive of the non-ignored files in the project.

    Args:
        project (``Project``): the project
        filename (str): name of a zip, tar.gz, or tar.bz2 archive file

    Returns:
        a ``Status``, if failed has ``errors``
    """
    return bundler._bundle_project(project, filename)


def upload(project, site=None):
    """Upload the project to the Anaconda server.

    Args:
        project (``Project``): the project
        site (str): site alias from Anaconda config

    Returns:
        a ``Status``, if failed has ``errors``
    """
    # delete=True breaks on windows if you use tmp_tarfile.name to re-open the file,
    # so don't use delete=True.
    # future: change suffix to .tar.bz2 once server can handle it...
    tmp_tarfile = tempfile.NamedTemporaryFile(delete=False, prefix="anaconda_upload_", suffix=".tar")
    tmp_tarfile.close()  # immediately un-use it to avoid file-in-use errors on Windows
    try:
        status = bundle(project, tmp_tarfile.name)
        if not status:
            return status
        status = client._upload(project, tmp_tarfile.name, site=site)
        return status
    finally:
        os.remove(tmp_tarfile.name)
