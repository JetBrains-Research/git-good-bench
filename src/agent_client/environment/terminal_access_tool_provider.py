import ast
import logging
from typing import Optional, List
import os

from docker.models.containers import Container
from pydantic import Field

from src.agent_client.environment.scenario_environment_manager import ScenarioEnvironmentManager
from src.agent_client.environment.scenario_type import ScenarioType
from src.agent_client.utils.exceptions import ScenarioEnvironmentException

class TerminalAccessToolImplementationProvider:
    DEFAULT_ERROR: str = "ERROR: Could not execute given command."

    def __init__(
            self,
            container: Container,
            error_message: Optional[str],
            bash_timeout: Optional[int],
            max_num_chars_bash_output: Optional[int],
            workdir: str,
            scenario_environment_manager: ScenarioEnvironmentManager
    ):
        super().__init__()

        self.error_message = error_message or self.DEFAULT_ERROR
        self.bash_timeout = bash_timeout
        self.max_num_chars_bash_output = max_num_chars_bash_output
        self.container = container
        self.workdir = workdir
        self.scenario_environment_manager = scenario_environment_manager

    def commit_changes_in(self,
                          selected_hunks: List[int] = Field(
                               description="A list of hunk ids. The changes of all hunks in identified through this list will be committed together in a single commit. "
                                        "A hunk id is the number N in HUNK-N in the list of remaining hunks."
                           ),
                          commit_message: str = Field(
                               description='A clear and descriptive commit message with which the commit containing the changes in the selected hunks is to be created.'
                           ),
                          reason: str = Field(
                               description="A reason why you are calling the tool. For example, 'to change required gradle version' or 'to specify the java sdk'."
                           )):
        """
        Processes the changes in the selected hunks by applying them to the current HEAD and creating a commit with the specified commit message, the
         patch file is updated to contain all hunks that still need to be processed.
        """
        file = 'file_changes.patch'
        number_of_remaining_hunks, x = self.scenario_environment_manager.get_remaining_hunks(file)

        if any([int(h) > number_of_remaining_hunks for h in selected_hunks]):
            return f'One or more selected hunks are out of range. The maximum number of hunks is {number_of_remaining_hunks}.'

        self.scenario_environment_manager.cut_selected_hunks_from_file(selected_hunks, file)

        try:
            err_code, output = self._apply_and_commit_changes(commit_message, file)

            self.scenario_environment_manager.run_git_diff(options=f'HEAD {self.scenario_environment_manager.scenario["newest_commit"]} -- '
                                                                           f'{self.scenario_environment_manager.scenario["file"]} > {file}')
            if err_code != 0:
                raise RuntimeError(f"Failed to update remaining hunks in {file}: {output}")

            number_of_remaining_hunks, remaining_hunks = self.scenario_environment_manager.get_remaining_hunks(file)

            if not remaining_hunks:
                with open(f'{self.scenario_environment_manager.host_agent_work_dir}/all_changes.patch', 'r') as f:
                    remaining_changes = f.read()

            return (f'Successfully created commit and applied changes from selected hunks. '
                    f'The total number of hunks that require processing now is {number_of_remaining_hunks}.'
                    f' The indices and content of the remaining chunks were updated to:\n{remaining_hunks}') \
                if remaining_hunks else ('Successfully created commit and applied changes from selected hunks. '
                                         f'The total number of hunks that require processing now is {number_of_remaining_hunks}. '
                                         f'All hunks are processed, you are done. '
                                         f'You must now call commit_remaining_changes to commit the remaining changes below: {remaining_changes}')

        except Exception as e:
            logging.error(f"An error occurred while committing the selected hunks: {str(e)}")
            return f"Failed to process the selected hunks. Unexpected exception, please terminate."

    def _apply_and_commit_changes(self, commit_message, file, termination_mode:bool=False):
        """
        Applies the passed Git patch file, stages the changes, and commits them with a given commit message.
        If termination_mode is True, the function will remove internal scenario patch files to avoid committing them.
        Raises a RuntimeError if any of the Git commands fail during execution.

        Args:
            commit_message (str): Commit message for the Git commit.
            file (str): Path to the patch file that should be applied.
            termination_mode (bool, optional): Indicates whether the function is called during termination. Defaults to False.

        Returns:
            tuple: A tuple containing the error code (int) and the output (str) from the
                Git commit command.

        Raises:
            RuntimeError: If applying the patch, staging changes, or creating the commit fails.
        """
        apply_patch_command = f'git apply --allow-empty --whitespace=fix {file}'
        err_code, output = self.container.exec_run(apply_patch_command, workdir=self.workdir, privileged=False)
        output = output.decode("utf-8")

        if err_code != 0:
            raise RuntimeError(f"Failed to apply the patch: {output}")

        if termination_mode:
            os.remove(f'{self.scenario_environment_manager.host_agent_work_dir}/all_changes.patch')
            os.remove(f'{self.scenario_environment_manager.host_agent_work_dir}/file_changes.patch')

            if 'all_changes.patch' in os.listdir(self.scenario_environment_manager.host_agent_work_dir) or \
                    'file_changes.patch' in os.listdir(self.scenario_environment_manager.host_agent_work_dir):
                raise RuntimeError(f"Failed to remove internal scenario patch files: {output}")

            stage_changes_command = f'git add {self.scenario_environment_manager.repository_work_dir}'
            err_code, output = self.container.exec_run(stage_changes_command, workdir=self.workdir, privileged=False)
        else:
            stage_changes_command = f'git add {self.scenario_environment_manager.scenario["file"]}'
            err_code, output = self.container.exec_run(stage_changes_command, workdir=self.workdir, privileged=False)

        output = output.decode("utf-8")

        if err_code != 0:
            raise RuntimeError(f"Failed to stage changes: {output}")

        create_commit_command = f'git commit -m "{commit_message}"'
        err_code, output = self.container.exec_run(create_commit_command, workdir=self.workdir, privileged=False)
        output = output.decode("utf-8")

        if err_code != 0:
            raise RuntimeError(f"Failed to create commit: {output}")

        return err_code, output

    def commit_remaining_changes(self,
                                               commit_message: str = Field(
                              description='A clear and descriptive commit message with which the commit containing the changes in the selected hunks is to be created.'
                          ),
                            reason: str = Field(
                              description="A reason why you are calling the tool. For example, 'to change required gradle version' or 'to specify the java sdk'."
                          )):
        """
        Once you have received the signal that you successfully processed all remaining hunks in the iterative committing of changes scenario, you must use this tool to commit the remaining changes and finish the scenario.
        Must only be used after commit_changes_in informs you of the scenario's completion in the iterative committing of changes scenario.
        """
        file = 'all_changes.patch'

        try:
            if self.scenario_environment_manager.scenario_type.value != ScenarioType.FILE_COMMIT_CHAIN_CHUNK.value:
                return f'This tool is invalid for the your current scenario type: {self.scenario_environment_manager.scenario_type.value}'

            if self.scenario_environment_manager.scenario['purity'] < 1:
                self._apply_and_commit_changes(commit_message, file, termination_mode=True)

            return 'You successfully committed all remaining changes. This scenario is complete and you must now terminate.'

        except Exception as e:
            logging.error(f"An error occurred while committing the remaining changes outside the scenario's file: {str(e)}")
            return f"An error occurred while committing the remaining changes outside the scenario's file. Unexpected exception, please terminate."

    def view_rebase_todo(self):
        """
            Inspect the current git rebase todo list.
        """
        return self.scenario_environment_manager.view_rebase_todo()

    def update_rebase_todo_list(self,
                          rebase_todo_list_items: List[str] = Field(
                               description="A list of strings conforming to the rebase-todo-list-item schema. "
                                           "The list must contain exactly one rebase-todo-list-item for every original commit. "
                                           " A rebase todo item is a dict the key of which is the index i of the commit, and the "
                                           "value of which is a dict containing the 'command' and a 'commit_msg' that is valid only for some commands. Refer to the command definitions above for valid commands and args. "
                                           "The index i refers to the ith commit in the list of commits you were shown above. If the rebase todo item's position in this list and they commit index in the dict do not match "
                                           "a swap in the ordering of rebase todo list items will occur."
                           ),
                          reason: str = Field(
                               description="A reason why you are calling the tool. For example, 'to change required gradle version' or 'to specify the java sdk'."
                           )):
        """
            Updates the git rebase todo list based on the provided rebase_todo_items. Exactly those rows, for which a rebase todo item is
            passed will be updated to the new command that will be executed with the provided commit_msg if valid for this command.
        """
        rebase_todo_list_items = [ast.literal_eval(rebase_todo_list_item) for rebase_todo_list_item in rebase_todo_list_items]
        try:
            update_succeeded, status = self.scenario_environment_manager.update_rebase_todo_commit_abstraction_map(rebase_todo_list_items)
            if update_succeeded and status == '':
                return f'Successfully updated rebase todo list. Rebase todo list after the update:\n{self.scenario_environment_manager.view_rebase_todo()}'
            else:
                return status
        except IndexError as e:
            return f'One of the commit_index values you specified is out of range.\nIndexError: {e}'

    def execute_rebase(self,
                       reason: str = Field(
                           description="A reason why you are calling the tool. For example, 'to change required gradle version' or 'to specify the java sdk'."
                       )):
        """
            Executes the rebase with the current git rebase todo list. This should be called once you are satisfied with
            the commands in the rebase todo list and would like to actually update the local tree by executing those commands.
        """
        try:
            return self.scenario_environment_manager.execute_rebase()
        except ScenarioEnvironmentException as e:
            return (f'Executing the rebase did not work. You failed to resolve this scenario, you must now terminate.'
                    f'The following error was raised: {str(e)}')

    def show_changes_in(self,
                        commit_index: int = Field(
                            description="Index of the commit with respect to the current rebase todo list that you would like to inspect the changes of."
                        ),
                        reason: str = Field(
                           description="A reason why you are calling the tool. For example, 'to change required gradle version' or 'to specify the java sdk'."
                        )):
        """
            Inspect all changes contained in the commit with the specific index. Will contain the path to the file in which the changes were made and some context around the changes.
        """
        try:
            return f'The changes introduced by commit {commit_index} are:\n{self.scenario_environment_manager.show_changes_in(commit_index)}'
        except IndexError as e:
            return (f'Fetching the changes for commit {commit_index} did not work. Incorrect index.'
                    f'The following error was raised: {str(e)}')
        except ScenarioEnvironmentException as e:
            return (f'Could not fetch changes in commit {commit_index}.'
                    f'The following error was raised: {str(e)}')

    def view_current_merge_conflict_with(self,
                                         context_window_size: int = Field(
                            description="The amount of lines around the current merge conflict to include for additional context. "
                                        "Is bounded by the available content in the file. For example, if the merge conflict is at the "
                                        "beginning of the file less than context_window_size lines may be included before the conflicting region.",
                            default=5
                        ),
                                         reason: str = Field(
                            description="A reason why you are calling the tool. For example, 'to change required gradle version' or 'to specify the java sdk'."
                        )):
        """
            Returns the current merge conflict that you must currently resolve. Will contain the path to the file in which the conflict was found,
            and the actual conflict.
        """
        current_merge_conflict_index =  len(self.scenario_environment_manager.unresolved_merge_conflicts) - \
            len(self.scenario_environment_manager.all_conflicts)
        return self.scenario_environment_manager.view_conflict_at(current_merge_conflict_index, context_window_size)

    def view_merge_conflict_at(self,
                 conflict_index: int = Field(
                     description="The index of the merge conflict that you would like to inspect the content of. "
                                 "Must be in the range [0, MAX_CONFLICT_INDEX] where MAX_CONFLICT_INDEX is the maximum "
                                 "index of the merge conflicts that you were originally tasked to solve above.",
                 ),
                 context_window_size: int = Field(
                     description="The amount of lines around the current merge conflict to include for additional context. "
                                 "Is bounded by the available content in the file. For example, if the merge conflict is at the "
                                 "beginning of the file less than context_window_size lines may be included before the conflicting region.",
                     default=5
                 ),
                 reason: str = Field(
                     description="A reason why you are calling the tool. For example, 'to change required gradle version' or 'to specify the java sdk'."
                 )):
        """
            Returns the content of the merge conflict with the specific index. Will contain the path to the file in which the conflict was found,
            and the actual conflict.
        """
        return self.scenario_environment_manager.view_conflict_at(conflict_index, context_window_size)

    def resolve_current_merge_conflict_with(self,
                                            content: str = Field(
                         description="A string containing the content to which you would like to resolve the current conflict. This is the data that the section in "
                                     "the file containing the content will be overwritten with. Only the region of the actual conflict delimited by the "
                                     "git conflict markers '<<<<<<<', '>>>>>>>', including the lines of the conflict markers, will be overwritten with the content."
                     ),
                                            reason: str = Field(
                         description="A reason why you are calling the tool. For example, 'to change required gradle version' or 'to specify the java sdk'."
                     )):
        """
            Allows you to resolve the current merge conflict by passing the content that the conflict should be overwritten with.
        """
        return self.scenario_environment_manager.resolve_current_merge_conflict_with(content)

    def view_file_at(self,
                 relative_path_from_project_root: str = Field(
                     description="The relative path from the project root of the file that you want to view the content of."
                 ),
                 reason: str = Field(
                     description="A reason why you are calling the tool. For example, 'to change required gradle version' or 'to specify the java sdk'."
                 )):
        """
            Allows you to view the content of a file. Returns that file's entire content.
        """
        try:
            return self.scenario_environment_manager.view_file_at(relative_path_from_project_root)
        except ScenarioEnvironmentException as e:
            return (f'Could not fetch file at {relative_path_from_project_root}.'
                    f'The following error was raised: {str(e)}')

    def view_diff_for(self,
                     relative_path_from_project_root: str = Field(
                         description="The relative path from the project root of the file that you want to view the git diff of."
                     ),
                     reason: str = Field(
                         description="A reason why you are calling the tool. For example, 'to change required gradle version' or 'to specify the java sdk'."
                     )):
        """
            Allows you to view the difference between the parent commits which are being merged in this scenario,
            with respect to the file located at relative_path_from_project_root.
        """
        return self.scenario_environment_manager.view_diff_between_merge_conflict_commits_for(relative_path_from_project_root)
