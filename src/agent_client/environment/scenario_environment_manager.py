import copy
import logging
import os
import re
import subprocess
from collections import deque
from datetime import datetime
from time import sleep
from typing import Optional, List, Tuple

from docker.models.containers import Container

from src.agent_client.environment.scenario_type import ScenarioType
from src.agent_client.utils.available_context import AvailableContext
from src.agent_client.utils.exceptions import ScenarioEnvironmentException
from src.data_processing_scripts.schemas import SampleDataRowV4


class ScenarioEnvironmentManager:
    AGENT_TARGET_BRANCH_NAME = 'current-scenario-branch'
    VALID_REBASE_COMMANDS = ['pick', 'drop', 'fixup', 'fixup -C', 'squash', 'reword']
    MAX_REBASE_RETRIES = 5

    def __init__(self,
                 container: Container,
                 sample: SampleDataRowV4,
                 host_agent_work_dir: str,
                 scenario_type: Optional[ScenarioType] = None,
                 scenario: Optional[dict] = None):
        self.container = container
        self.sample = sample
        self.host_agent_work_dir = host_agent_work_dir
        self.repository_name = sample.name
        self.scenario_type = scenario_type
        self.scenario = scenario
        self.repository_work_dir = self._get_repository_working_directory()
        self.default_branch_name = None
        self.command_template = '/bin/bash -c "{command_to_execute}"'
        self.commit_abstraction_mapping = []
        self.prepend_break_sequence_editor_script = '../sequence_editor.sh'
        self.unresolved_merge_conflicts = deque()
        self.all_conflicts = self.unresolved_merge_conflicts
        self._successfully_setup_agent_branch = False

    def set_scenario(self, scenario: dict):
        self.scenario = scenario

    def set_scenario_type(self, scenario_type: ScenarioType):
        self.scenario_type = scenario_type

    def setup_scenario_preconditions(self):
        """
        Sets up the preconditions for different scenario types.

        Depending on the scenario type, this method will either:
          - Set up iteratively chunk staged diff into commits.
          - Set up a clean local branch before push.

        For all scenario types a new branch with the name in self.AGENT_TARGET_BRANCH_NAME is created and checked out to
        isolate the agent actions from the rest of the repository.

        Raises:
            NotImplementedError: If the scenario type is not supported.
            ScenarioEnvironmentException: If scenario or scenario_type are not initialized. Also, if the branch setup failed.
        """
        if self.scenario is None:
            raise ScenarioEnvironmentException('Cannot setup scenario, since scenario is None.')

        if self.scenario_type is None:
            raise ScenarioEnvironmentException('Cannot setup scenario, since scenario_type is None.')

        if self.scenario_type is ScenarioType.FILE_COMMIT_CHAIN_CHUNK:
            self._setup_iteratively_chunk_staged_diff_into_commits()
        elif self.scenario_type is ScenarioType.FILE_COMMIT_CHAIN_REBASE:
            self._setup_clean_local_branch_before_push()
        elif self.scenario_type is ScenarioType.MERGE or self.scenario_type is ScenarioType.CHERRY_PICK:
            self._setup_merge_conflict_scenario()
        else:
            raise NotImplementedError(
                f'Currently only supporting ScenarioType.{ScenarioType.FILE_COMMIT_CHAIN_CHUNK.name}'
                f'and ScenarioType.{ScenarioType.FILE_COMMIT_CHAIN_REBASE.name}.')

    def teardown_scenario(self):
        """
        Resets the repository to its default state after a scenario has been executed or if an error occurred.

        This involves resetting staged changes, resetting the working directory, and removing the agent's target branch.
        Upon successful completion, validates that the target branch has been removed. For rebase scenarios,
        the rebase is also aborted.

        Raises:
            ScenarioEnvironmentException: If the reset operation fails or if the
            target branch is still present after the reset.
        """
        teardown_command = ('git reset --hard HEAD && ' # Reset any changes staged or unstaged and workdir, also aborts dangling rebase implicitly
                            f'git checkout -f {self.default_branch_name}')

        if self._successfully_setup_agent_branch:
            # Remove the branch in which the agent attempted to solve this scenario
            # and force removal from the repository entirely, by triggering garbage collection
            # Reading Note: `git prune` could end up being to costly to run after every scenario.
            #   Monitor this.
            # Reading Note: In case of a crash, the branch may not yet be set up, which would cause the teardown to fail. To
            #   account for this edge case we use this flag.
            teardown_command += f' && git branch -D {self.AGENT_TARGET_BRANCH_NAME} && git prune'

        if self.scenario_type is ScenarioType.FILE_COMMIT_CHAIN_REBASE:
            self.commit_abstraction_mapping = []

            rebase_in_progress_err_code, rebase_in_progress_output = self.container.exec_run(
                '/bin/bash -c "git status"', privileged=False, workdir=self.repository_work_dir)

            if 'rebase in progress' in rebase_in_progress_output.decode('utf-8'):
                teardown_command = 'git rebase --abort && ' + teardown_command

        teardown_command_err_code, teardown_output = self.container.exec_run(
            '/bin/bash -c "{command_to_execute}"'\
                .format(command_to_execute=teardown_command),
            privileged=False, workdir=self.repository_work_dir)
        validation_command_err_code, validation_output = self.container.exec_run(
            '/bin/bash -c "{command_to_execute}"'.format(command_to_execute=f'git branch --list {self.AGENT_TARGET_BRANCH_NAME}'),
            privileged=False, workdir=self.repository_work_dir)
        if teardown_command_err_code == 0 and validation_command_err_code == 0 and validation_output == b'':
            logging.info(f'Successfully tore down the scenario.')
        else:
            raise ScenarioEnvironmentException(f"Could not reset repository. Command output: {teardown_output.decode('utf-8')}."
                                               f"\nBranch deletion validation (empty string if successful): {validation_output.decode('utf-8')}"
                                               f"\nDocker error code: {teardown_command_err_code}.")

    def setup_repository(self):
        """
        Clones the repository and sets up the default branch name.

        This method performs the initial setup of the repository by cloning it
        to the local machine. It also retrieves and sets the default branch name
        for the repository.

        Raises:
            ScenarioEnvironmentException: If either the cloning or setup of the default branch name fail.
        """
        logging.info(f'Cloning repository: {self.repository_name} to {self.repository_work_dir}.')
        self._clone_repository()
        self._setup_git_lfs()
        self.default_branch_name = self._get_default_branch_name()

    def teardown_repository(self):
        """
        Remove the repository from the container.

        Removes the repository directory with `rm -r` and then lists the remaining files for debugging purposes.

        Raises:
            ScenarioEnvironmentException: If the repository could not be reset, indicated by a non-zero Docker error code.
        """
        err_code, output = self.container.exec_run(
            '/bin/bash -c "{command_to_execute}"'.format(command_to_execute=f'rm -r {self.repository_work_dir} && ls'),
            privileged=False)
        if err_code == 0:
            logging.info(f'Successfully removed repository: {self.repository_name} from container.')
            logging.debug(f'"ls" yields: {output.decode("utf-8")}')
        else:
            raise ScenarioEnvironmentException(f"Could not reset repository. Docker error code: {err_code}.")

    def provide_scenario_context(self, requested_contexts: List[AvailableContext]):
        """
        Dynamically provides contexts for different scenarios based on the requested contexts.

        Args:
            requested_contexts (List[AvailableContext]): A list of context types to provide.

        Returns:
            dict: A dictionary containing the requested contexts. The keys are the enum members.

        Raises:
            ScenarioEnvironmentException: If the context cannot be fetched (any command fails).
        """
        provided_contexts = {}
        for requested_context in requested_contexts:
            if requested_context == AvailableContext.GIT_STATUS:
                provided_contexts[AvailableContext.GIT_STATUS] = self._run_git_status()
            elif requested_context == AvailableContext.GIT_DIFF:
                provided_contexts[AvailableContext.GIT_DIFF] = self.run_git_diff()
            elif requested_context == AvailableContext.REMAINING_HUNKS:
                provided_contexts[AvailableContext.REMAINING_HUNKS] = self.get_remaining_hunks('file_changes.patch')
            elif requested_context == AvailableContext.REBASE_PARTICIPATING_COMMITS:
                provided_contexts[AvailableContext.REBASE_PARTICIPATING_COMMITS] = self._get_rebase_participating_commits()
            elif requested_context == AvailableContext.COMMIT_TEMPORAL_ORDERING:
                provided_contexts[AvailableContext.COMMIT_TEMPORAL_ORDERING] = self._get_temporal_ordering_of_merge_parent_commits()
            elif requested_context == AvailableContext.COMMIT_TYPE:
                provided_contexts[AvailableContext.COMMIT_TYPE] = self._get_commit_types_for_cherry_pick_scenario()
            elif requested_context == AvailableContext.TOTAL_AMOUNT_OF_MERGE_CONFLICTS:
                provided_contexts[AvailableContext.TOTAL_AMOUNT_OF_MERGE_CONFLICTS] = len(self.unresolved_merge_conflicts)
            elif requested_context == AvailableContext.FILES_WITH_CONFLICTS:
                provided_contexts[AvailableContext.FILES_WITH_CONFLICTS] = self._get_files_with_conflicts()
            elif requested_context == AvailableContext.ALL_MERGE_CONFLICTS:
                provided_contexts[AvailableContext.ALL_MERGE_CONFLICTS] = self._get_all_merge_conflicts()

        return provided_contexts

    def _clone_repository(self):
        """
        Clones the git repository of the current repository into the container.

        The repository URL is formed using the `self.repository_name` attribute.
        If the clone operation fails (non-zero error code), an error message
        is logged. Otherwise, the output of the clone operation is logged
        as an info message.

        Raises:
            ScenarioEnvironmentException if the clone operation fails.
        """
        # Executes the startup command in a blocking way, ensuring that the repository is available before continuing
        clone_command = '/bin/bash -c "git clone https://github.com/{repository_name}.git"'
        err_code, output = self.container.exec_run(clone_command.format(repository_name=self.repository_name))

        output = output.decode("utf-8")
        if err_code != 0:
            raise ScenarioEnvironmentException(f'Could not clone repository {self.repository_name}:\n{output}')
        else:
            logging.info(f'Successfully cloned repository {self.repository_name}:\n{output}')

    def _get_default_branch_name(self):
        """
        Retrieves the default branch name from the output of the "git status" command. Run right after cloning the
        repository.

        Returns:
            str: The name of the default branch.

        Raises:
            ScenarioEnvironmentException: If the output of "git status" does not contain the branch information or if the output is empty.
        """
        output = self._run_git_status()

        lines = output.splitlines()
        if len(lines) > 0:
            first_line = lines[0]
            if 'On branch' in first_line:
                return first_line.split('On branch ')[1].strip()
            else:
                raise ScenarioEnvironmentException(f'"git status" did not contain default branch: {output}')
        else:
            raise ScenarioEnvironmentException(f'Cannot parse "git status" output, nothing to parse: {output}')

    def _setup_agent_branch(self):
        """
        Sets up the branch isolating the agent's actions from the rest of the repository.

        This method creates and checks out a new branch specified by AGENT_TARGET_BRANCH in the
        repository residing in the Docker container.

        Raises:
            ScenarioEnvironmentException: If the branch setup and checkout fail, an exception is
                                          raised with the Docker error code.

        """
        err_code, output = self.container.exec_run(
            '/bin/bash -c "{command_to_execute}"'.format(command_to_execute=f'git checkout -b "{self.AGENT_TARGET_BRANCH_NAME}"'),
            privileged=False, workdir=self.repository_work_dir)
        if err_code == 0:
            logging.info(f'Successfully set up branch for agent actions: {self.AGENT_TARGET_BRANCH_NAME}.')
            logging.debug(f'"git status" after setup:\n{self._run_git_status()}')
            self._successfully_setup_agent_branch = True
        else:
            raise ScenarioEnvironmentException(f"Could not set up and check out agent branch: {self.AGENT_TARGET_BRANCH_NAME}. "
                                               f"Docker error code: {err_code}.")

    def _run_git_status(self):
        """
        Returns:
            str: The output of the `git status` command if successful.

        Raises:
            ScenarioEnvironmentException: If the execution of the `git status` command in the Docker container fails.
        """
        err_code, output = self.container.exec_run(
            '/bin/bash -c "{command_to_execute}"'.format(command_to_execute='git status'),
            privileged=False, workdir=self.repository_work_dir)
        if err_code == 0:
            return output.decode("utf-8")
        else:
            raise ScenarioEnvironmentException(f"Cannot get git status. Docker error code: {err_code}.")

    def run_git_diff(self, options: str = ''):
        """
        Args:
            options (str): Options to execute the 'git diff' command with, will be appended with a whitespace.

        Returns:
            str: The output of the `git diff` command if successful.

        Raises:
            ScenarioEnvironmentException: If the execution of the `git diff` command in the Docker container fails.
        """
        err_code, output = self.container.exec_run(
            '/bin/bash -c "{command_to_execute}"'.format(command_to_execute=f'git diff {options}'),
            privileged=False, workdir=self.repository_work_dir)
        if err_code == 0:
            return output.decode("utf-8")
        else:
            raise ScenarioEnvironmentException(f"Failed to run git diff. Docker error code: {err_code}.")

    def get_remaining_hunks(self, file: str) -> Tuple[int, str]:
        """
        Provides the remaining hunks as delimited by @@ in all_changes.patch as a string, replacing
        the hunk delimiters with annotations 'HUNK-N:'. Excludes the diff header/metadata information.

        Args:
            file (str): The name of the patch file from which to parse the hunks

        Returns:
            int: The number of remaining hunks in the file
            str: Formatted string containing the remaining hunks.

        Raises:
            ScenarioEnvironmentException: If it fails to read all_changes.patch file.
        """
        try:
            err_code, output = self.container.exec_run(
                f"cat {file}",
                privileged=False,
                workdir=self.repository_work_dir
            )
            if err_code != 0:
                raise ScenarioEnvironmentException(f"Failed to read {file}.")

            patch_content = output.decode("utf-8")
            lines = patch_content.splitlines()

            if len(lines) == 0:
                return 0, ''

            formatted_hunks = []
            hunk_counter = 1
            have_found_first_hunk = False

            for line in lines:
                if line.startswith('@@'):
                    formatted_hunks.append(f"\nHUNK-{hunk_counter}:")
                    hunk_counter += 1
                    have_found_first_hunk = True
                elif not have_found_first_hunk:
                    # Skip diff header/metadata lines and lines before the first hunk
                    continue
                else:
                    formatted_hunks.append(line)

            return hunk_counter-1, "\n".join(formatted_hunks)
        except Exception as e:
            raise ScenarioEnvironmentException(f"Error processing {file}: {str(e)}")

    def cut_selected_hunks_from_file(self, selected_hunks: List[int], file: str):
        """
        Provides the remaining hunks as delimited by @@ in all_changes.patch as a string, replacing
        the hunk delimiters with annotations 'HUNK-N:'. Excludes the diff header/metadata information.

        Args:
            selected_hunks:
            file (str): The name of the patch file from which to parse the hunks

        Returns:
            str: Formatted string containing the remaining hunks.

        Raises:
            ScenarioEnvironmentException: If it fails to read all_changes.patch file.
        """
        try:
            err_code, output = self.container.exec_run(
                f"cat {file}",
                privileged=False,
                workdir=self.repository_work_dir
            )
            if err_code != 0:
                raise ScenarioEnvironmentException(f"Failed to read {file}.")

            patch_content = output.decode("utf-8")
            diff_header, hunks = patch_content.split("@@", 1)
            # Prepend the delimiter to restore the header of the first hunk, part of which was consumed when splitting
            hunks = '@@' + hunks

            hunk_pattern = re.compile(r"((?:@@.+\n(?: .*(?:\n)?|\+.*(?:\n)?|\-.*(?:\n)?|(?:(?:\n|\\))?)*))", re.MULTILINE)
            hunks = hunk_pattern.findall(hunks)

            selected_hunks = [hunks[i - 1] for i in selected_hunks if 0 < i <= len(hunks)]
            extracted_data = ''.join([diff_header] + selected_hunks)

            with open(file, 'w+') as f:
                f.write(extracted_data)

            subprocess.run(['docker', 'cp', f'{file}', f'{self.container.id}:{self.repository_work_dir}/{file}'])

            return 0
        except Exception as e:
            raise ScenarioEnvironmentException(f"Error getting contents of {file}: {str(e)}")

    def _setup_git_lfs(self):
        """
        This is needed to cleanly setup/teardown scenarios in repos with git LFS enabled.

        Raises:
            ScenarioEnvironmentException: If git-lfs setup fails.
        """
        git_lfs_command = '/bin/bash -c "apt-get update && apt install git-lfs"'
        err_code, output = self.container.exec_run(git_lfs_command, workdir=self.repository_work_dir, privileged=False)
        if err_code != 0:
            raise ScenarioEnvironmentException('Could not setup git LFS. This is needed to cleanly setup/teardown '
                                               f'scenarios in repos with git LFS enabled:\n{output.decode("utf-8")}')

    def _get_repository_working_directory(self):
        """
        Gets the repository working directory inside the container.

        This method runs a shell command to get the present working directory inside the container.
        It appends the repository name (sans any preceding path) to this directory and sets the repository
        working directory for the instance and returns the result. Does not require the repository to be cloned already.

        Raises:
            ValueError: If the working directory can't be determined.

        Returns:
            str: Absolute path to the repository working directory for the repository.
        """
        err_code, output = self.container.exec_run("/bin/bash -c pwd")
        if err_code == 0:
            return output.decode("utf-8").strip() + '/' + self.repository_name.split("/")[-1]
        else:
            raise ValueError("Can't determine working directory.")

    def _setup_iteratively_chunk_staged_diff_into_commits(self):
        """
        We first check out the chronologically newest (first) commit and get the changes that were made to the file of the
        scenario between the newest and oldest (last) commit and store the patch in all_changes.patch. Then
        we checkout the oldest commit and restore the worktree state of the file of the scenario to the state in the oldest commit.
        All changes in the chain of commits are then stored in all_changes.patch and the state of the file is the state
        of it at the oldest commit. At this point hunks from all_changes.patch can be applied.

        Raises:
            ScenarioEnvironmentException: If an error occurs during checkout or reset commands within the Docker container.
        """
        setup_command = (f"git checkout {self.scenario['newest_commit']} && "
                         f"git reset HEAD~{self.scenario['times_seen_consecutively']} && "
                         f"git diff > all_changes.patch && "
                         f"git checkout -f {self.scenario['newest_commit']} && "
                         f"git reset HEAD~{self.scenario['times_seen_consecutively']} -- {self.scenario['file']} && "
                         f"git diff > file_changes.patch && "
                         f"git checkout -f HEAD~{self.scenario['times_seen_consecutively']} && "
                         f"git restore {self.scenario['file']}")

        err_code, output = self.container.exec_run(self.command_template.format(command_to_execute=setup_command),
                                                   privileged=False, workdir=self.repository_work_dir)

        # Process patch files to remove changes in file_changes.patch from all_changes.patch
        all_changes_path = f'{self.host_agent_work_dir}/all_changes.patch'
        file_changes_path = f'{self.host_agent_work_dir}/file_changes.patch'

        try:
            # Read the patch files
            with open(all_changes_path, 'r') as f:
                all_changes_content = f.read()

            with open(file_changes_path, 'r') as f:
                file_changes_content = f.read()

            # Process only if both files have content
            if all_changes_content.strip() and file_changes_content.strip():
                # Get the filename from self.scenario['file']
                target_file = self.scenario['file']

                # Split the content into sections based on diff headers
                sections = []
                current_section = []
                current_section_has_target = False

                for line in all_changes_content.splitlines(True):  # Keep line endings
                    if line.startswith('diff --git'):
                        # Save the previous section if it exists and doesn't contain the target file
                        if current_section and not current_section_has_target:
                            sections.extend(current_section)

                        # Start a new section
                        current_section = [line]
                        current_section_has_target = target_file in line
                    else:
                        current_section.append(line)

                # Add the last section if it doesn't contain the target file
                if current_section and not current_section_has_target:
                    sections.extend(current_section)

                # Use the filtered sections as the result
                result_lines = sections

                # Write the updated content back to all_changes.patch
                with open(all_changes_path, 'w') as f:
                    f.writelines(result_lines)

        except Exception as e:
            raise ScenarioEnvironmentException(f"Error setting up patch files: {str(e)}")
        if err_code != 0:
            raise ScenarioEnvironmentException(f"Cannot set up scenario. Docker error "
                                               f"code: {err_code}\n\nOutput: {output.decode('utf-8')}.")
        else:
            logging.info(f'Scenario precondition for {self.scenario_type} successfully set up.')
            self._setup_agent_branch()

    def _checkout_commit(self, commit: str):
        """
        Args:
            commit: The specific commit hash or identifier to be checked out in the git repository.

        Raises: ScenarioEnvironmentException: If the checkout command fails.
        """
        checkout_command = f"git checkout {commit}"
        err_code, output = self.container.exec_run(self.command_template.format(command_to_execute=checkout_command),
                                                   privileged=False, workdir=self.repository_work_dir)
        if err_code != 0:
            raise ScenarioEnvironmentException(f"Cannot check out commit: {commit}. "
                                               f"Docker error code: {err_code}.")

    def _setup_clean_local_branch_before_push(self):
        """
        Checks out the first (ie. chronologically newest) commit in the scenario and initiates a rebase of the last
        time_consecutively_seen commits. The rebase-todo file that opens is immediately closed agin to maintain access
        to the terminal.

        Raises:
            ScenarioEnvironmentException: If the checkout command fails.
        """
        self._checkout_commit(self.scenario['newest_commit'])
        self._setup_agent_branch()
        rebase = f"GIT_SEQUENCE_EDITOR={self.prepend_break_sequence_editor_script} git rebase -i HEAD~{self.scenario['times_seen_consecutively']}"
        err_code, output = self.container.exec_run(self.command_template.format(command_to_execute=rebase),
                                                   privileged=False, workdir=self.repository_work_dir)
        if err_code != 0:
            raise ScenarioEnvironmentException(f"Could not initiate rebase for past {self.scenario['times_seen_consecutively']} commits."
                                               f"Docker error code: {err_code}, output: {output}")
        else:
            logging.info(f'Scenario precondition for {self.scenario_type} successfully set up.')

    def _setup_merge_conflict_scenario(self):
        """
        Checks out the first parent commit in the scenario and creates a branch to isolate the agent's actions to.
        Initiates the merge of cherry pick and parses the data in the resulting merge conflict output to populate
        self.unresolved_merge_conflicts with all merge conflicts that the agent must resolve.

        Note that which parent is checked out may impact system performance on these samples, since LLMs
        are sensitive to ordering.

        Raises:
            ScenarioEnvironmentException: If the checkout command fails.
            NotImplementedError: If an invalid scenario type is configured in self.scenario_type.
        """
        self._checkout_commit(self.scenario['parents'][0])
        self._setup_agent_branch()
        if self.scenario_type is not ScenarioType.MERGE:
            raise NotImplementedError(f'Setting up merge conflict scenario for {self.scenario_type} failed. Only merge scenario is supported.')

        self._attempt_merge()

        for unmerged_path in self.scenario['files_in_merge_conflict']:
            self.extract_sections_with_conflict_in(unmerged_path)

        self.all_conflicts = copy.deepcopy(list(self.unresolved_merge_conflicts))

    def extract_unmerged_paths_from(self, raw_conflict_overview: str) -> List[str]:
        unmerged_paths = []
        for line in raw_conflict_overview.splitlines():
            if line.startswith("CONFLICT"):
                unmerged_path_match = re.search(r'([\w\-.]+\/)*[\w\-.]+\.(?:java|kt|py)', line)
                if unmerged_path_match:
                    unmerged_paths.append(unmerged_path_match[0])

        return unmerged_paths

    def _attempt_cherry_pick(self):
        cherry_pick_command = f"git cherry-pick {self.scenario['cherry_commit']}"

        err_code, output = self.container.exec_run(self.command_template.format(command_to_execute=cherry_pick_command),
                                                   privileged=False, workdir=self.repository_work_dir)
        if 'CONFLICT' in output.decode("utf-8"):
            return output.decode("utf-8")
        else:
            raise ScenarioEnvironmentException(
                f"Could not initiate cherry-pick. No merge conflict occurred. Docker error code: {err_code}, output: {output.decode('utf-8')}.")

    def _attempt_merge(self):
        participating_parent_commits = ' '.join(self.scenario['parents'][1:])
        merge_command = f"git merge {participating_parent_commits}"

        err_code, output = self.container.exec_run(self.command_template.format(command_to_execute=merge_command),
                                                   privileged=False, workdir=self.repository_work_dir)
        if err_code == 1 and 'CONFLICT' in output.decode("utf-8"):
            return output.decode("utf-8")
        elif err_code == 0:
            # In case of no conflict, the extraction will simply result in 0 conflicts and the agent will be told
            # to terminate.
            return output.decode("utf-8")
        else:
            raise ScenarioEnvironmentException(
                f"Could not initiate merge. No merge conflict occurred. Docker error code: {err_code}, output: {output.decode('utf-8')}.")

    def _get_rebase_todo_contents(self, timeout:int = 60):
        path_to_rebase_directory = os.path.join(os.getcwd(), 'agent_work_dir',self.repository_name.split('/')[-1] ,'.git/rebase-merge')
        while not os.path.exists(path_to_rebase_directory):
            sleep(1)
            timeout -= 1
            if timeout == 0:
                raise ScenarioEnvironmentException('rebase-merge directory not found. A rebase is not in progress.')

        with open(os.path.join(path_to_rebase_directory, 'git-rebase-todo'), 'r') as rebase_todo_file:
            return rebase_todo_file.readlines()

    def _initialize_commit_abstraction_mapping(self):
        """
            Initializes self.commit_abstraction_mapping to the content of the original rebase-todo file (all commands
            will be 'pick').

            Furthermore, the order of the commits in this list map to the order of the todos in
            the rebase-todo file. Thus the ith-commit that the agent may want to update refers to the index of this list.
        """
        rebase_todo_contents = self._get_rebase_todo_contents()
        self.commit_abstraction_mapping = []

        for l, line in enumerate(rebase_todo_contents):
            abstraction_map = {}

            if 'fixup -C' in line:
                results = re.match(r'^(\S+)\s+(\S+)\s+(\S+)\s+(.*)$', line.strip())
                if results:
                    abstraction_map['command'] = results.group(1) + ' ' + results.group(2)
                    abstraction_map['commit'] = results.group(3)
                    abstraction_map['commit_msg'] = results.group(4)
            else:
                results = re.match(r'^(\S+)\s+(\S+)\s+(.*)$', line.strip())
                if results:
                    abstraction_map['command'] = results.group(1)
                    abstraction_map['commit'] = results.group(2)
                    abstraction_map['commit_msg'] = results.group(3)

            abstraction_map['target_command'] = abstraction_map['command']
            self.commit_abstraction_mapping.append(abstraction_map)

    def update_rebase_todo_commit_abstraction_map(self, target_rebase_todo_list):
        # TODO does this also work if the agent wants to re-order commits?
        if not self.commit_abstraction_mapping:
            self._initialize_commit_abstraction_mapping()

        if len(target_rebase_todo_list) != len(self.commit_abstraction_mapping):
            return False, ('Amount of specified rebase todo list items did not match original amount. Original: '
                           f'{len(self.commit_abstraction_mapping)}, Specified via "rebase_todo_items" parameter: {len(target_rebase_todo_list)}')

        new_mapping = []
        for target_rebase_todo_list_item in target_rebase_todo_list:
            if target_rebase_todo_list_item['commit_index'] >= len(self.commit_abstraction_mapping):
                raise IndexError(f'Item with "commit_index": {target_rebase_todo_list_item["commit_index"]}'
                          f' was out of the range of valid indices. Valid max index: {len(self.commit_abstraction_mapping)-1}.')

            if target_rebase_todo_list_item['command'] not in self.VALID_REBASE_COMMANDS:
                return False, f'Command {target_rebase_todo_list_item["command"]} is not supported. Aborting. Valid commands are:\n{self.VALID_REBASE_COMMANDS}'

            # Note: This approach is necessary to maintain the mapping to the actual rebase-todo file that we only update
            # and once before the execution of the rebase in execute_rebase
            new_mapping.append(self.commit_abstraction_mapping[target_rebase_todo_list_item['commit_index']])
            new_mapping[-1]['target_command'] = target_rebase_todo_list_item['command']
            if 'commit_msg' in target_rebase_todo_list_item:  # Only update commit message if provided
                new_mapping[-1]['commit_msg'] = target_rebase_todo_list_item['commit_msg']

        self.commit_abstraction_mapping = new_mapping

        return True, ''

    def view_rebase_todo(self):
        if not self.commit_abstraction_mapping:
            self._initialize_commit_abstraction_mapping()

        rebase_todo_output = ''

        for item in self.commit_abstraction_mapping:
            rebase_todo_output += f"{item['target_command']} {item['commit']} {item['commit_msg']}\n"

        return rebase_todo_output

    def execute_rebase(self):
        if os.path.exists(os.path.join(self.host_agent_work_dir, '.git/rebase-merge')):

            rebase_todo_list = ''
            for rebase_todo_item in self.commit_abstraction_mapping:
                if rebase_todo_item['target_command'] in ['pick','drop','fixup','fixup -C']:
                    # Can just execute, these are safe commands that dont hang (by opening the editor)
                    rebase_todo_list += f"{rebase_todo_item['target_command']} {rebase_todo_item['commit']} {rebase_todo_item['commit_msg']}\n"
                elif rebase_todo_item['target_command'] in ['reword']:
                    rebase_todo_list += f"pick {rebase_todo_item['commit']} {rebase_todo_item['commit_msg']}\n"
                    rebase_todo_list += f"exec git commit --amend -m '{rebase_todo_item['commit_msg']}'\n"
                elif rebase_todo_item['target_command'] in ['fixup -c']:
                    rebase_todo_list += f"fixup -C {rebase_todo_item['commit']} {rebase_todo_item['commit_msg']}\n"
                elif rebase_todo_item['target_command'] in ['squash']:
                    rebase_todo_list += f"fixup {rebase_todo_item['commit']} {rebase_todo_item['commit_msg']}\n"
                    rebase_todo_list += f"exec git commit --amend -m '{rebase_todo_item['commit_msg']}'\n"

            with open(os.path.join(self.host_agent_work_dir, '.git/rebase-merge/git-rebase-todo'), 'w') as rebase_todo_file:
                rebase_todo_file.write(rebase_todo_list)

        success_message = 'You successfully performed the interactive rebase, you can now terminate.'
        rebase = f"git rebase --continue"
        err_code, output = self.container.exec_run(self.command_template.format(command_to_execute=rebase),
                                                   privileged=False, workdir=self.repository_work_dir)

        if err_code != 0:
            for i in range (0, self.MAX_REBASE_RETRIES):
                err_code, output = self.container.exec_run(self.command_template.format(command_to_execute='git status'),
                                                           privileged=False, workdir=self.repository_work_dir)

                if err_code == 0 and 'interactive rebase in progress' in output.decode('utf-8'):
                    if 'all conflicts fixed' in output.decode('utf-8'):
                        err_code, output = self.container.exec_run(self.command_template.format(command_to_execute=rebase),
                            privileged=False, workdir=self.repository_work_dir)
                        if err_code == 0:
                            return success_message
                    else:
                        raise ScenarioEnvironmentException(
                            f"Rebase started but did not complete. Recovery failed and unexpected error occurred. "
                            f"Docker error code: {err_code}, output: {output.decode('utf-8')}.")
                elif err_code == 0:
                    return success_message
                else:
                    raise ScenarioEnvironmentException(
                        f"Could not perform rebase, rebase not in progress. Docker error code: {err_code}, output: {output.decode('utf-8')}.")
        else:
            return success_message

    def extract_sections_with_conflict_in(self, path_to_unmerged_file: str):
        with open(os.path.join(self.host_agent_work_dir, path_to_unmerged_file), 'r') as unmerged_file:
            unmerged_lines = unmerged_file.readlines()

            # Note this implementation does not provide context around the conflict, but the agent was given this by
            #  in the prompt
            for line_number, line in enumerate(unmerged_lines):
                if '<<<<<<<' in line:
                    conflicting_section = {'file': path_to_unmerged_file, 'begin_line': line_number, 'end_line': 0,
                                           'file_content': unmerged_lines}
                elif '>>>>>>>' in line:
                    conflicting_section['end_line'] = line_number
                    self.unresolved_merge_conflicts.append(conflicting_section)
                else:
                    continue

    def show_changes_in(self, commit_index: int):
        show_commit = f"git show {self.commit_abstraction_mapping[commit_index]['commit']}"
        err_code, output = self.container.exec_run(self.command_template.format(command_to_execute=show_commit),
                                                   privileged=False, workdir=self.repository_work_dir)
        if err_code != 0:
            raise ScenarioEnvironmentException(
                f"Could not fetch changes. Docker error code: {err_code}, output: {output}.")
        else:
            return output.decode("utf-8")

    def _get_rebase_participating_commits(self):
        if not self.commit_abstraction_mapping:
            self._initialize_commit_abstraction_mapping()

        participating_commits = ''
        for i, item in enumerate(self.commit_abstraction_mapping):
            show_commit = f"git show {item['commit']}"
            err_code, output = self.container.exec_run(self.command_template.format(command_to_execute=show_commit),
                                                       privileged=False, workdir=self.repository_work_dir)
            if err_code != 0:
                raise ScenarioEnvironmentException(
                    f"Could not fetch detailed context for commits participating in rebase. Docker error code: {err_code}, output: {output}.")
            else:
                participating_commits += f'<COMMIT-{i}>\n'
                participating_commits += output.decode("utf-8") + '\n'
                participating_commits += f'</COMMIT-{i}>\n'

        return participating_commits

    def view_file_at(self, path):
        host_path = os.path.join(self.host_agent_work_dir, path)
        if not os.path.exists(host_path):
            raise ScenarioEnvironmentException(f'Path {path} does not exist.')

        with open(host_path, 'r') as f:
            file_content = f.read()
            return file_content

    def resolve_current_merge_conflict_with(self, content: str):
        next_merge_conflict = self.unresolved_merge_conflicts.popleft()

        with (open(os.path.join(self.host_agent_work_dir, next_merge_conflict['file']), 'r+') as f):
            file_content = f.readlines()
            # Ensure that all lines produced by the LLM end with a newline so that we dont accidentally shift the content
            # upwards by removing an empty line and break our indexation.
            content = [l + '\n' for l in content.splitlines() if not l.endswith('\n')]
            result = file_content[:next_merge_conflict['begin_line']] + \
                    content + \
                    file_content[next_merge_conflict['end_line'] + 1:] # end_line is the line containing the closing delimiter of the conflict, this must also be replaced
            f.seek(0)
            f.writelines(result)
            f.truncate()

        # If it is the last conflicting section, we are done and can complete the command that resulted in the conflict
        if len(self.unresolved_merge_conflicts) == 0:
            #In both cases we need to add the changes to staging
            err_code, output = self.container.exec_run(self.command_template.format(command_to_execute="git add ."),
                                                       privileged=False, workdir=self.repository_work_dir)
            if err_code != 0:
                raise ScenarioEnvironmentException(
                    f"Could not stage the files in which merge conflicts were resolved. Docker error code: {err_code}, output: {output}.")
            else:
                if self.scenario_type is ScenarioType.MERGE:
                    err_code, output = self.container.exec_run(
                        self.command_template.format(command_to_execute='git commit -m \"Merge after resolving conflicts\"'),
                        privileged=False, workdir=self.repository_work_dir)
                    if err_code != 0:
                        raise ScenarioEnvironmentException(
                            f"Could not stage the files in which merge conflicts were resolved. Docker error code: {err_code}, output: {output}.")
                    else:
                        return 'Successfully resolved merge conflict in merge. No conflicts remaining, you must now terminate.'
                elif self.scenario_type is ScenarioType.CHERRY_PICK:
                    err_code, output = self.container.exec_run(
                        self.command_template.format(command_to_execute='git cherry-pick --continue'),
                        privileged=False, workdir=self.repository_work_dir)
                    if err_code != 0:
                        raise ScenarioEnvironmentException(
                            f"Could not stage the files in which merge conflicts were resolved. Docker error code: {err_code}, output: {output}.")
                    else:
                        return 'Successfully resolved merge conflict in cherry pick. No conflicts remaining, you must now terminate.'
        else:
            # How big of a chunk are we removing? With how big of a chunk are we patching what we removed?
            # The difference of these two is the offset to shift the remaining conflicts in the file by
            # Note that we start with and including the begin_line, thus we are starting to count at 0 and must add 1
            index_offset = (len(content)) - (next_merge_conflict['end_line'] - next_merge_conflict['begin_line'] + 1)
            for unresolved_merge_conflict in self.unresolved_merge_conflicts:
                if unresolved_merge_conflict['file'] == next_merge_conflict['file']:
                    unresolved_merge_conflict['begin_line'] = unresolved_merge_conflict['begin_line'] + index_offset \
                        if unresolved_merge_conflict['begin_line'] + index_offset >= 0 else 0
                    unresolved_merge_conflict['end_line'] = unresolved_merge_conflict['end_line'] + index_offset \
                        if unresolved_merge_conflict['end_line'] + index_offset <= len(result) - 1 else len(result) - 1

        return f'Successfully applied resolution to merge conflict. {len(self.unresolved_merge_conflicts)} remaining. You must now move on to the next conflict.'

    def view_diff_between_merge_conflict_commits_for(self, path: str):
        view_diff = f'git diff {" ".join(self.scenario["parents"])} -- {path}'
        err_code, output = self.container.exec_run(self.command_template.format(command_to_execute=view_diff),
                                                   privileged=False, workdir=self.repository_work_dir)
        if err_code != 0:
            raise ScenarioEnvironmentException(
                f"Could not compute diff between the scenario's parents {self.scenario['parents']} for {path}.\n"
                f"Docker error code: {err_code}, output: {output}.")
        else:
            return output.decode("utf-8")

    def _show_commit_timestamp(self, commit_hash: str):
        show_commit_timestamp = f"git show -s --format=%ct {commit_hash}"
        err_code, output = self.container.exec_run(self.command_template.format(command_to_execute=show_commit_timestamp),
                                                   privileged=False, workdir=self.repository_work_dir)
        if err_code != 0:
            raise ScenarioEnvironmentException(
                f"Could not commit timestamp. Docker error code: {err_code}, output: {output}.")
        else:
            return output.decode("utf-8")

    def _get_temporal_ordering_of_merge_parent_commits(self):
        timestamps = {}
        for parent in self.scenario["parents"]:
            timestamps[parent] = datetime.fromtimestamp(int(self._show_commit_timestamp(parent)))

        days_difference = abs((timestamps[self.scenario["parents"][0]] - timestamps[self.scenario["parents"][1]]).days)

        temporal_ordering_context = ('"<<<<<<< HEAD" side in the merge conflict represents the local changes and '
                                      'the ">>>>>>>" side in the merge conflict represents the incoming changes.\n')

        # We merge parent[1] into parent[0], so parent[0] is the HEAD
        if timestamps[self.scenario["parents"][0]] >= timestamps[self.scenario["parents"][1]]:
            temporal_ordering_context += (f'The local changes (ie. the content below <<<<<<<) are {days_difference} days NEWER '
                                          f'than the incoming changes (ie. the content below >>>>>>>).')
        else:
            temporal_ordering_context += (f'The incoming changes (ie. the content below >>>>>>>) are {days_difference} days NEWER '
                                          f'than the local changes (ie. the content below <<<<<<<).')

        return temporal_ordering_context

    def _get_commit_types_for_cherry_pick_scenario(self):
        # We pick the cherry into the parent (ie the dst)
        return (f'The "<<<<<<< HEAD" side in the merge conflict represents the destination cherry-pick commit and '
                f'the ">>>>>>>" side (ie. incoming changes) in the merge conflict represents the cherry commit to be '
                f'picked into the destination cherry-pick commit.')

    def _get_files_with_conflicts(self):
        """
        Extracts unique files from unresolved_merge_conflicts without removing elements.

        Returns:
            str: A string containing the unique file paths that have merge conflicts, separated by newlines.
        """
        unique_files = {conflict['file'] for conflict in self.unresolved_merge_conflicts}
        return '\n'.join(sorted(unique_files))

    def _get_all_merge_conflicts(self):
        """
        Parses self.unresolved_merge_conflicts without removing elements to extract the regions where merge conflicts are located.
        For each conflict, extracts the region using begin_line and end_line indices from file_content.

        Returns:
            str: A string containing all merge conflicts, each delimited by <CONFLICT-i> tags where i is 0-based index.
        """
        all_conflicts = ''
        for i, conflict in enumerate(self.unresolved_merge_conflicts):
            # Add 1 to end_line when slicing to include the end line
            conflict_content = ''.join(conflict['file_content'][conflict['begin_line']:conflict['end_line'] + 1])
            all_conflicts += f'<CONFLICT-{i}>\n'
            all_conflicts += f'File: {conflict["file"]}\n'
            all_conflicts += conflict_content
            all_conflicts += f'</CONFLICT-{i}>\n'
        return all_conflicts

    def view_conflict_at(self, conflict_index: int, context_window_size: int):
        """
        Parses self._all_conflicts[conflict_index] to extract the region where the merge conflict
         is located. The returned region will contain context_window_size lines around the conflict.

        Returns:
            str: A string containing the file in which the conflict occurs and the conflict with optional context.
        """
        if conflict_index < 0 or conflict_index >= len(self.all_conflicts):
            return (f'The index {conflict_index} is out of range. Please specify a valid index in range '
                    f'[0, {len(self.all_conflicts) - 1}].')

        file = self.all_conflicts[conflict_index]['file']
        max_context_idx = len(
            self.all_conflicts[conflict_index]['file_content'])
        conflict_with_context = self.all_conflicts[conflict_index][
                                    'file_content'][
                                self.all_conflicts[conflict_index][
                                    'begin_line'] - context_window_size if
                                self.all_conflicts[conflict_index][
                                    'begin_line'] - context_window_size >= 0 else 0
                                :
                                self.all_conflicts[conflict_index][
                                    'end_line'] + context_window_size + 1 if
                                self.all_conflicts[conflict_index][
                                    'begin_line'] + context_window_size + 1 <= max_context_idx else max_context_idx
                                ]
        return (f'CONFLICT-{conflict_index} was found in: {file} and the section with the conflict including '
                f'{context_window_size} lines of context is:\n{"".join(conflict_with_context)}')
