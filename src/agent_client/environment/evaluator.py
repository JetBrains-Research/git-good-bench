import ast
import json
import os
import warnings
from typing import Optional

from docker.models.containers import Container

from src.agent_client.environment.scenario_type import ScenarioType
from src.agent_client.utils.exceptions import ScenarioEnvironmentException

class Evaluator:

    EVALUATE_FILE_COMMIT_CHAIN_SYSTEM_PROMPT = """Please act as an impartial judge and evaluate the quality of the two
    git histories that are displayed below. Your evaluation should consider the following aspects:
    - The quality of the commit messages with respect to consistency, conciseness, duplication and correctness with respect to the content of the commit.
    - The logical cohesion of the changes present within the commits. Changes in a commit should have high logical cohesion.
    - The logical progression and common thread between the commits and especially the order in which the commits are presented.
    - The size of the commits. Commits should be as small as possible without breaking the system (e.g. changing a method signature 
        in a non-backwards compatible way without also changing all uses of the method in the same commit).

    Your job is to evaluate which git history is of higher quality. Avoid any position biases and ensure that the order 
    in which the responses were presented does not influence your decision. Do not allow the length of the responses to 
    influence your evaluation. Be as objective as possible. 

    You must adhere to the response format demonstrated in example responses below:
    {{
        'evaluation_result': 'HISTORY-1', 
        'evaluation_reason': 'The first git history has more descriptive commit and non-duplicate messages that align
            much more accurately with the content of the commits.' 
    }}
    {{
        'evaluation_result': 'HISTORY-2', 
        'evaluation_reason': 'The commits in git history 2 are more concise and introduce logically coherent changes. 
            The changes are introduces in such a way that they are unlikely to break the system as the commits are self-contained 
            with respect to the part of the system that they affect and correctly propagate changes throughout the system. 
            Thus I chose history 2 despite it having poorer quality commit messages.' 
    }}
    {{
        'evaluation_result': 'TIE', 
        'evaluation_reason': 'Both histories introduces changes that are logically coherent and have similar commit messages. 
            None of the two histories have fundamental issues, such as duplicate commit messages or changes that obviously 
            would break the system if they were introduced as presented. As I am unsure, I am declaring a tie.' 
    }}
    """

    EVALUATE_FILE_COMMIT_CHAIN_PROMPT_USER_TEMPLATE = """
        <HISTORY-1>
{history_1}
    </HISTORY-1>

    <HISTORY-2>
{history_2}
    </HISTORY-2>"""

    response_schema = {
        "type": "json",
        "schemaName": "judge_histories_response",
        "schema": {
            "type": "object",
            "properties": {
                "evaluation_result": {"enum": ["HISTORY-1", "HISTORY-2", "TIE"]},
                "evaluation_reason": {"type": "string"}
            },
            "required": ["evaluation_result, evaluation_reason"],
            "additionalProperties": False
        }
    }


    def __init__(self,
                 container: Container,
                 agent_target_branch_name: str,
                 repository_work_dir: str,
                 llm_client,
                 host_agent_work_dir: str,
                 scenario_type: Optional[ScenarioType] = None,
                 scenario: Optional[dict] = None):
        self.container = container
        self.agent_target_branch_name = agent_target_branch_name
        self.repository_work_dir = repository_work_dir
        self.llm_client = llm_client
        self.host_agent_work_dir = host_agent_work_dir
        self.scenario_type = scenario_type
        self.scenario = scenario
        self.command_template = '/bin/bash -c "{command_to_execute}"'

        self._agent_solution = None
        self._ground_truth = None
        self._llm_responses = None

    def get_evaluation_metadata(self):
        return {
            'agent_solution': self._agent_solution,
            'ground_truth': self._ground_truth,
            'llm_responses': self._llm_responses
        }

    def set_scenario(self, scenario: dict):
        self.scenario = scenario

    def set_scenario_type(self, scenario_type: ScenarioType):
        self.scenario_type = scenario_type

    def evaluate(self) -> bool:
        """
            Evaluates the configured scenario.

            Raises:
                NotImplementedError: If the scenario type is not supported.
                ScenarioEnvironmentException: If scenario or scenario_type are not initialized or the evaluation failed.

            Returns:
                bool: True if the scenario was successfully and correctly solved, False otherwise.
        """
        if self.scenario is None:
            raise ScenarioEnvironmentException('Cannot evaluate scenario, since scenario is None.')

        if self.scenario_type is None:
            raise ScenarioEnvironmentException('Cannot evaluate scenario, since scenario_type is None.')

        if self.scenario_type is ScenarioType.FILE_COMMIT_CHAIN_CHUNK:
            return self._evaluate_iteratively_chunk_staged_diff_into_commits()
        elif self.scenario_type is ScenarioType.FILE_COMMIT_CHAIN_REBASE:
            return self._evaluate_clean_local_branch_before_push()
        elif self.scenario_type is ScenarioType.MERGE:
            return self._evaluate_diff_between_head_and(self.scenario['merge_commit_hash'])
        elif self.scenario_type is ScenarioType.CHERRY_PICK:
            return self._evaluate_diff_between_head_and(self.scenario['cherry_pick_commit'])
        else:
            raise NotImplementedError('Not supporting other scenario types.')

    def _evaluate_iteratively_chunk_staged_diff_into_commits(self):
        """
        Raises:
            ScenarioEnvironmentException: If a git operation fails during the process.

        Returns:
            bool: True if the evaluation indicates the agent's history is preferred over the ground truth by the Judge,
                and False otherwise.
        """
        return self._run_llm_as_a_judge_evaluation_for_git_histories()

    def _run_llm_as_a_judge_evaluation_for_git_histories(self):
        """
        Executes evaluation for git histories using a language model as a judge, comparing an agent's git
        commit history to a ground truth history. The function involves constructing both histories,
        generating evaluation prompts, and interpreting responses to determine a final judgment.

        We prompt the LLM twice to take a conservative approach to the result and avoid position bias.

        Raises:
            ScenarioEnvironmentException: If a git operation (checkout or log fetching) fails during the
            process.

        Returns:
            bool: True if the evaluation indicates the agent's history is preferred over the ground truth by the Judge,
                and False otherwise.
        """
        # Build history for ground truth
        err_code, output = self.container.exec_run(self.command_template.format(
            command_to_execute=f'git checkout {self.scenario["newest_commit"]}'),
            privileged=False, workdir=self.repository_work_dir)

        if err_code != 0:
            raise ScenarioEnvironmentException(
                f"Failed to checkout initial commit of ground truth git history: {output.decode('utf-8')}")

        err_code, output = self.container.exec_run(self.command_template.format(
            command_to_execute=f'git log --format=%H -n {self.scenario["times_seen_consecutively"]}'),
            privileged=False, workdir=self.repository_work_dir)

        if err_code != 0:
            raise ScenarioEnvironmentException(
                f"Failed to fetch commits for ground truth git history: {output.decode('utf-8')}")

        commits = output.decode("utf-8").strip().split('\n')
        ground_truth_history = self._build_git_history(commits)

        # Build history for agent
        # If we skip exactly times_seen_consecutively commits, we land at the newest commit outside of our scenario
        err_code, output = self.container.exec_run(self.command_template.format(
            command_to_execute=f'git log --format=%H -n 1 --skip={self.scenario["times_seen_consecutively"]}'),
            privileged=False, workdir=self.repository_work_dir)

        if err_code != 0:
            raise ScenarioEnvironmentException(
                f"Failed to fetch exit condition for building the agent's git history: {output.decode('utf-8')}")

        exit_commit = output.decode("utf-8").strip()

        err_code, output = self.container.exec_run(self.command_template.format(
            command_to_execute=f'git checkout {self.agent_target_branch_name}'),
            privileged=False, workdir=self.repository_work_dir)

        if err_code != 0:
            raise ScenarioEnvironmentException(
                f"Failed to checkout initial commit of agent git history: {output.decode('utf-8')}")

        agent_commits = []
        i = 0
        while True:
            err_code, output = self.container.exec_run(self.command_template.format(
                command_to_execute=f'git log --format=%H -n 1 --skip={i}'),
                privileged=False, workdir=self.repository_work_dir)
            i += 1

            if err_code != 0:
                raise ScenarioEnvironmentException(
                    f"Failed to get agent commit history commits: {output.decode('utf-8')}")

            current_commit = output.decode("utf-8").strip()

            if current_commit == exit_commit:
                break

            agent_commits.append(output.decode("utf-8").strip())

        agent_history = self._build_git_history(agent_commits)

        # Prompt twice to avoid position bias
        prompt_agent_gt = Chat().add_system(self.EVALUATE_FILE_COMMIT_CHAIN_SYSTEM_PROMPT).add_user(
            self.EVALUATE_FILE_COMMIT_CHAIN_PROMPT_USER_TEMPLATE.format(
                history_1='\n'.join(agent_history),
                history_2='\n'.join(ground_truth_history)
            )
        )
        prompt_gt_agent = Chat().add_system(self.EVALUATE_FILE_COMMIT_CHAIN_SYSTEM_PROMPT).add_user(
            self.EVALUATE_FILE_COMMIT_CHAIN_PROMPT_USER_TEMPLATE.format(
                history_1='\n'.join(ground_truth_history),
                history_2='\n'.join(agent_history)
            )
        )
        response_agent_gt = self._prompt_model_with(prompt_agent_gt)
        response_agent_gt = ast.literal_eval(response_agent_gt.content)
        response_gt_agent = self._prompt_model_with(prompt_gt_agent)
        response_gt_agent = ast.literal_eval(response_gt_agent.content)

        self._ground_truth = '\n'.join(ground_truth_history)
        self._agent_solution = '\n'.join(agent_history)
        self._llm_responses = json.dumps(
            [
                {'response_1': response_agent_gt, 'history_1': 'AGENT', 'history_2': 'GROUND_TRUTH'},
                {'response_2': response_gt_agent, 'history_1': 'GROUND_TRUTH', 'history_2': 'AGENT'}
            ])

        return response_agent_gt['evaluation_result'] == 'HISTORY-1' and response_gt_agent[
            'evaluation_result'] == 'HISTORY-2'

    def _prompt_model_with(self, prompt: Chat):
        """
        Abstraction wrapper for LLM backend communication.

        Args:
            prompt (Chat): The chat prompt containing the input for the language model.

        Returns:
            Any: The response from the language model after processing the given prompt.
        """
        return self.llm_client.chat(
            chat=prompt,
            profile=LLM ..,
            parameters={
                LLMParameters.Temperature: Parameters.FloatValue(0),
                LLMParameters.ResponseFormat: Parameters.JsonValue(self.response_schema),
            }
        )

    def _build_git_history(self, commits):
        """
        Builds the git history by executing git show on each commit and collecting the outputs.

        This method communicates with a container to retrieve the details of all specified Git commits.
        For FILE_COMMIT_CHAIN_CHUNK scenarios, it filters the output to only include changes related
        to the file specified in self.scenario['file']. The filtering preserves commit metadata
        (commit message, author, date) while only including diff information for the specified file.
        For other scenario types, the complete git show output is included.

        We perform this filtering because ScenarioType.FILE_COMMIT_CHAIN_CHUNK scenarios challenge the agent
        to work with the changes from a specific file in the commit only.

        If an error occurs during the execution of a command, an exception is raised that includes
        the error message.

        Args:
            commits (List[str]): A list of commit hashes for which the Git history is to be built.

        Raises:
            ScenarioEnvironmentException: If the container fails to execute the Git command for any commit.

        Returns:
            List[str]: A list of strings containing the details of each commit.
        """
        git_history = []
        for commit in commits:
            err_code, output = self.container.exec_run(self.command_template.format(
                command_to_execute=f'git show {commit}'),
                privileged=False, workdir=self.repository_work_dir)

            if err_code != 0:
                raise ScenarioEnvironmentException(
                    f"Failed to build git history {output.decode('utf-8')}")

            if self.scenario_type is ScenarioType.FILE_COMMIT_CHAIN_CHUNK:
                # Filter the output to only include changes for the specific file
                decoded_output = output.decode("utf-8").strip()
                lines = decoded_output.split('\n')
                filtered_lines = []
                in_target_file_diff = False
                have_encountered_first_diff = False

                for line in lines:
                    # Always include lines until we see the first diff
                    if not line.startswith('diff --git') and not have_encountered_first_diff:
                        filtered_lines.append(line)
                        continue

                    # Start of a new diff section
                    if line.startswith('diff --git'):
                        in_target_file_diff = False
                        have_encountered_first_diff = True

                        if f'diff --git a/{self.scenario["file"]} b/{self.scenario["file"]}' in line:
                            in_target_file_diff = True
                            filtered_lines.append(line)
                    # Include the line if we're in the target file's diff section
                    elif in_target_file_diff:
                        filtered_lines.append(line)

                git_history.append('\n'.join(filtered_lines))
            else:
                git_history.append(output.decode("utf-8").strip())
        return git_history

    def _evaluate_clean_local_branch_before_push(self):
        """
        Checks whether the agent successfully cleaned the local branch and then evaluates the git history produced
        by the agent by letting an LLM judge the git history compared to the ground truth history.

        Raises:
            ScenarioEnvironmentException: If there is an error executing the command inside the container.

        Returns:
            bool: True if the agent correctly executed the rebase and  if the evaluation indicates the agent's history
                is preferred over the ground truth by the Judge, and False otherwise.
        """
        err_code, output = self.container.exec_run(self.command_template.format(command_to_execute='git status'),
                                                   privileged=False, workdir=self.repository_work_dir)
        if err_code == 0 and 'interactive rebase in progress' in output.decode('utf-8'):
            # In this case a merge conflict occurred during a rebase and could not be resolved, leaving the rebase dangling
            err_code, _ = self.container.exec_run(self.command_template.format(command_to_execute='git rebase --abort'),
                                                       privileged=False, workdir=self.repository_work_dir)
            if err_code != 0:
                raise ScenarioEnvironmentException(f"Failed to abort dangling rebase with merge conflict: {output.decode('utf-8')}")
            return False

        return self._run_llm_as_a_judge_evaluation_for_git_histories()

    def _evaluate_diff_between_head_and(self, ground_truth_commit: str):
        """
        Checks whether the agent successfully performed the merge and did not introduce any unwanted changes
        when resolving a merge conflict.

        Evaluates to True if the state of the agent's branch HEAD is the same (diff is empty) as the ground truth merge commit.
        This implicitly also evaluates whether a merge was carried out at all, because if that is not the case, there
        would be a diff.

        Raises:
            ScenarioEnvironmentException: If there is an error executing the command inside the container.

        Returns:
            bool: True if the agent carried out the merge and did not introduce unwanted changes with respect
                to the ground truth merge commit, otherwise False.
        """
        err_code, output = self.container.exec_run(
            self.command_template.format(command_to_execute='git status'),
            privileged=False, workdir=self.repository_work_dir)

        if err_code != 0:
            raise ScenarioEnvironmentException(f"Cannot evaluate scenario: {output.decode('utf-8')}")
        # Reading Note: Some cases the merge conflict contains conflicts due to renaming, the agent is not currently able to solve these cases
        elif 'unmerged' in output.decode('utf-8').lower() or 'conflict' in output.decode('utf-8').lower():
            # Ensure that the command from which the conflict originated was cleanly carried out
            return False
        else:
            err_code, output = self.container.exec_run(
                self.command_template.format(command_to_execute=self._get_git_diff_evaluation_command(ground_truth_commit)),
                privileged=False, workdir=self.repository_work_dir)

            if err_code != 0:
                raise ScenarioEnvironmentException(f"Cannot evaluate scenario: {output.decode('utf-8')}")

            diff = output.decode("utf-8").strip()

            try:
                self._agent_solution = self._get_state_of_files_with_conflicts()

                err_code, output = self.container.exec_run(self.command_template.format(
                        command_to_execute=f'git checkout {ground_truth_commit}'),
                        privileged=False, workdir=self.repository_work_dir)

                if err_code != 0:
                    warnings.warn(f"Failed to check out ground truth commit {ground_truth_commit}.\n"
                                      f"Cannot collect ground truth metadata for current scenario.", UserWarning)
                else:
                    self._ground_truth = self._get_state_of_files_with_conflicts()
            except Exception as e:
                warnings.warn(f"Failed to collect evaluation metadata for current scenario: {e}", UserWarning)

            return diff == ''

    def _get_state_of_files_with_conflicts(self):
        """
        Reads the content of files in a merge conflict and returns their state in stringified JSON format.
        Note that the content of the files is based on the current commit that is checkout out in the container/repo.

        Returns:
            str: A stringified JSON where each item is a dictionary representing a file
            in merge conflict and its corresponding content.

        Raises:
            FileNotFoundError: If any of the files in merge conflict is not found.
            IOError: If there is an I/O-related error when opening or reading the files.
        """
        states = []
        for file in self.scenario['files_in_merge_conflict']:
            with open(os.path.join(self.host_agent_work_dir, file), 'r') as f:
                states.append({file: f.read()})

        return json.dumps(states)

    def _get_git_diff_evaluation_command(self, ground_truth_commit: str):
        """
        Returns the differences between the scenario's ground truth merge commit and the agent's target branch HEAD.

        Returns:
            str: The constructed shell command to execute the described git operations.
        """
        return f"git diff {ground_truth_commit} {self.agent_target_branch_name}"

    def _get_git_file_commit_chain_evaluation_command(self):
        """
        Generates a command to evaluate git file commit chain scenarios.

        Constructs a shell command to perform two git operations:
        1. Display differences between the scenario's newest commit and the agent's target branch HEAD for a given file.
        2. Count the number of commits between the scenario's oldest commit and the agent's target branch HEAD. The output is
            0-based, so it only counts commits ontop of self.scenario['oldest_commit'], excluding that commit.

        Returns:
            str: The constructed shell command to execute the described git operations.
        """
        return f"git diff {self.scenario['newest_commit']} {self.agent_target_branch_name}" \
                    f" -- {self.scenario['file']} && git rev-list --count {self.scenario['oldest_commit']}..{self.agent_target_branch_name}"

    def _can_be_cast_to_int(self, str):
        """
        Checks if a given string can be converted to an integer.

        Args:
            str: The input string to check if it can be converted to an integer.

        Returns:
            bool: True if the string can be converted to an integer, False otherwise.
        """
        try:
            int(str)
            return True
        except ValueError:
            return False
