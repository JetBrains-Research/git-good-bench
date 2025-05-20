import sys

from git import Repo, Commit, NULL_TREE, BadObject
import re
from queue import Queue
from tqdm import tqdm
from src.repository_data_scraper.programming_language import ProgrammingLanguage
import hashlib
from time import time
from typing import List, Dict
from warnings import warn


class RepositoryDataScraper:
    repository = None
    repository_name = None
    sliding_window_size = 3

    # Accumulates file-commit chains If we detect a series of n consecutive modifications of the same file we append a
    # dict to this list. Each dict contains: The associated file (relative path from working directory), first commit
    # for this file-commit chain, last commit for this file-commit chain and how many times the file was seen
    # consecutively (length of the file-commit chain) Note that the change_types that are valid are M, MM, A or R. All
    # other change types are ignored (because the file wasn't modified).
    accumulator = None

    # Maintains a state for each file currently in scope. Each scope is defined by the overlap size n, if we do not
    # see the file again after n steps we remove it from the state
    state = None

    visited_commits = None
    seen_commit_messages = None
    prochainming_language = None
    _cherry_pick_pattern = None

    def __init__(self, repository: Repo, programming_language: ProgrammingLanguage, repository_name: str,
                 sliding_window_size: int = 3):
        if repository is None:
            raise ValueError("Please provide a repository instance to scrape from.")

        self.repository = repository
        self.sliding_window_size = sliding_window_size
        self.programming_language = programming_language

        self.repository_name = repository_name

        self.accumulator = {'file_commit_chain_scenarios': [], 'merge_scenarios': [], 'cherry_pick_scenarios': []}
        self.state = {}
        self.branches = [ref.name for ref in self.repository.references if ('HEAD' not in ref.name)
                         and not ref.path.startswith('refs/tags')]

        self.visited_commits = set()
        self.seen_commit_messages = dict()

        # Based on the string appended to the commit message by the -x option in git cherry-pick
        self._cherry_pick_pattern = re.compile(r'(?<=cherry picked from commit )[a-z0-9]{40}')

    def update_accumulator_with_file_commit_chain_scenario(self, file_state: dict, file_to_remove: str, branch: str):
        """
        Updates the accumulator with the state at the given branch and file_to_remove with a file-commit chain scenario
        if the scenario at branch and file_to_remove is >= self.sliding_window_size long.

        Args:
            file_state: (dict): A dictionary containing the state of the file.
            file_to_remove (str): The name of the file to be removed.
            branch (str): The name of the branch where the file exists.
        """
        if file_state['times_seen_consecutively'] >= self.sliding_window_size:
            self.accumulator['file_commit_chain_scenarios'].append(
                {'file': file_to_remove, 'branch': branch, 'oldest_commit': file_state['oldest_commit'],
                 'newest_commit': file_state['newest_commit'],
                 'times_seen_consecutively': file_state['times_seen_consecutively']})

    def scrape(self):
        """
        Parses the repository to collect merge, cherry_pick and file_commit_chain scenarios.

        Iterates over all branches of a repository processing commits of the supported changes types:
            - M
            - MM
            - A

        The scenarios mined, are stored in self.accumulator. To optimize compute, we dont process commits that
        were already seen again. The exception is that we process past a branches' origin commit for
        self.sliding_window_size commits, to mine file-commit chains that overlap outside of a branch.
        """
        valid_change_types = ['A', 'M', 'MM']
        for branch in tqdm(self.branches, desc=f'Parsing branches in {self.repository_name}'):
            try:
                commit = self.repository.commit(branch)
            except Exception as e:
                if isinstance(e, BadObject):
                    warning_content = (
                        f'\nCould not get branch HEAD for branch {branch}. Branch probably contains "@". '
                        f'GitPython cant handle that.\n\nSkipping branch ...')
                    warn(warning_content, category=RuntimeWarning)
                    continue
                else:
                    raise e

            frontier = Queue(maxsize=0)
            frontier.put(commit)

            # If we hit a commit that was already covered by another branch, continue for
            # self.sliding_window_size - 1 commits to cover file-commit chains overlapping, with at least one
            # commit on the current branch
            keepalive = self.sliding_window_size - 1

            while not frontier.empty():
                commit = frontier.get()
                is_merge_commit = len(commit.parents) > 1
                merge_commit_sample = {}

                # Ensure we early stop if we run into a visited commit
                # This happens whenever this branch (the one currently being processed) joins another branch at
                # its branch origin, iff we have already processed  a branch running past this branch's origin,
                # meaning we visited this branch origin's commit thus all commits thereafter
                if commit.hexsha not in self.visited_commits:
                    self.visited_commits.add(commit.hexsha)

                    frontier = self._update_frontier_with(commit, frontier, is_merge_commit)
                elif keepalive > 0:
                    # If we hit a commit which we have already seen, it means we are hitting another branch
                    # To catch overlaps, we continue for keepalive commits
                    keepalive -= 1
                else:
                    # Now that we also handled overlaps, stop processing this branch
                    break

                self._process_cherry_pick_scenario(commit)

                changes_in_commit = self._get_changes_in_commit(commit)

                # At this point the commit metadata such as the message are trimmed
                # Each line represents one file that was changed. This means each line contains the change type and
                # relative filepath. Thus, it is safe to simply search list string for file endings.
                does_commit_contain_changes_in_programming_language = self._does_commit_contain_changes_in_programming_language(changes_in_commit)
                if does_commit_contain_changes_in_programming_language:
                    self._update_commit_message_tracker(commit)

                # If it is a merge with conflicts (ie introduced patch) ensure that the changes correspond to
                # the specified programming_language
                if is_merge_commit and (len(changes_in_commit) == 0 or
                                        does_commit_contain_changes_in_programming_language):
                    merge_commit_sample = {'merge_commit_hash': commit.hexsha, 'had_conflicts': False,
                                           'parents': [parent.hexsha for parent in commit.parents]}

                affected_files = []

                for change_in_commit in changes_in_commit:
                    changes_to_unpack = change_in_commit.split('\t')

                    # Only process valid change_types
                    if changes_to_unpack[0] not in valid_change_types:
                        continue

                    # Only maintain a state for files of required programming_language
                    change_type, file = changes_to_unpack
                    if self.programming_language.value not in file:
                        continue

                    affected_files.append(file)

                    if is_merge_commit and change_type == 'MM':
                        merge_commit_sample['had_conflicts'] = True

                    self._maintain_state_for_change_in_commit(branch, commit, file)
                self._remove_stale_file_states(affected_files, branch)

                if is_merge_commit and merge_commit_sample:
                    self.accumulator['merge_scenarios'].append(merge_commit_sample)

            self._handle_newest_commit_file_commit_chain_edge_case()

            # Clean up
            self.state = {}

        start = time()
        self.accumulator[
            'cherry_pick_scenarios'] += self._mine_commits_with_duplicate_messages_for_cherry_pick_scenarios()
        print(f'Extra time incurred: {round(time() - start, 4)}s', file=sys.stderr)

    def _does_commit_contain_changes_in_programming_language(self, changes_in_commit: List[str]):
        """
        Check if a commit contains changes in a specific programming language.

        Args:
            changes_in_commit (List[str]): A list of the changes in the commit.

        Return:
            True if any change in the commit changes a file of self.programming_language file ending
        """
        return any([(self.programming_language.value in changes_in_commit) for changes_in_commit in changes_in_commit])

    def _should_process_commit(self, changes_in_commit: List[str], valid_change_types: List[str]):
        """
        Checks if the commit contains any change of valid change type and programming language in the same change.
        Ie. a file has to be of self.programming_language and its change type must be in valid_change_types

        Args:
            changes_in_commit (List[str]): Changes in the commit.
            valid_change_types (List[str]): Each item represents a type of change that should be processed.

        Returns:
            A boolean value indicating whether the commit should be processed or not.
            True if any of the changes in the commit match any of the valid change types and the programming language
            of the commit matches the programming language for which we are scraping data, otherwise False.
        """
        is_any_change_type_valid = False
        for change in changes_in_commit:
            # Change types such as rename yield a list of length 3 here, cannot simply unpack in every case
            change_type = change.split('\t')[0]
            is_any_change_type_valid = (change_type in valid_change_types) and (
                    self.programming_language.value in change)
            if is_any_change_type_valid:
                return is_any_change_type_valid
        return is_any_change_type_valid

    def _handle_newest_commit_file_commit_chain_edge_case(self):
        """
        Handle the edge case where file-commit chains are still active, or continuing in the last commit. In this case we
        need to also update the accumulator with these scenarios to successfully mine them.

        After we are done with all commits, the state might contain valid file-commit chains
        lasting until and including the last commit (ie we have just seen the file and then terminate).
        To capture this edge case we need to iterate over the state one more time.
        """
        for tracked_branch in self.state:
            for file in self.state[tracked_branch]:
                self.update_accumulator_with_file_commit_chain_scenario(self.state[tracked_branch][file], file,
                                                                       tracked_branch)

    def _remove_stale_file_states(self, affected_files: List[str], branch: str):
        """
        Removes stale file states from the state of the given branch.

        Some file-commit chains might have stopped in this commit. If this is the case, we no longer need to maintain
        a state for them. If their length was >= self.sliding_window_size we should successfully mined a scenario
        and must update the accumulator with it.

        Args:
            affected_files (List[str]): List of files affected by the commit.
            branch (str): Branch affected by the commit.

        """
        # Now we only need to remove stale file states (files that were not found in the commit)
        # Only do this for branches affected by the commit
        if self.state:
            new_state = {}
            for file in self.state[branch]:
                if file in affected_files:
                    new_state[file] = self.state[branch][file]
                else:
                    self.update_accumulator_with_file_commit_chain_scenario(self.state[branch][file], file,
                                                                           branch)
            self.state[branch] = new_state

    def _maintain_state_for_change_in_commit(self, branch: str, commit: Commit, file: str):
        """
        Updates the state. Does not write any results to the accumulator.

        Initializes the state for a branch with a empty dict if we are not currently maintaining
        a state for this branch. Then keeps track of file-commit chains >= self.sliding_window_size

        Args:
            branch (str): The name of the branch where the commit occurred.
            commit (Commit): The Commit object representing the commit being made.
            file (str): The name of the file that was changed in the commit.

        """
        if branch not in self.state:
            self.state[branch] = {}

        if file in self.state[branch]:
            # We are maintaining a state for this file on this branch
            self.state[branch][file]['times_seen_consecutively'] = self.state[branch][file][
                                                                       'times_seen_consecutively'] + 1

            if self.state[branch][file]['times_seen_consecutively'] >= self.sliding_window_size:
                self.state[branch][file]['newest_commit'] = commit.hexsha
        else:
            # We are not currently maintaining a state for this file in this branch, but have
            # detected it Need to set up the state dict
            self.state[branch][file] = {'oldest_commit': commit.hexsha, 'newest_commit': commit.hexsha,
                                        'times_seen_consecutively': 1}

    def _get_changes_in_commit(self, commit: Commit) -> List:
        """
        Generates a list of changes in a commit using git show with arguments: name_status=True, format='oneline'.
        Contains only actual changes. Changes start with a change type followed by the affected file(s).
        Can affect multiple files for e.g. renaming.

        Args:
            commit (Commit): The commit object representing the commit for which changes are to be retrieved.

        Returns:
            List: A list of strings representing the changes in the given commit.
        """
        changes_in_commit = self.repository.git.show(commit, name_status=True, format='oneline').split('\n')
        changes_in_commit = changes_in_commit[1:]  # remove commit hash and message
        changes_in_commit = [change for change in changes_in_commit if change]  # filter empty lines
        return changes_in_commit

    def _process_cherry_pick_scenario(self, commit: Commit):
        """
        Checks the commit message for a cherry-pick scenario and, if present, adds it to the class's accumulator.

        This function does not return a value. Instead, it updates the class's accumulator with the following
             data structure:
            {
                'cherry_pick_commit': <commit hash (str)>,
                'cherry_commit': <matched cherry-pick commit (str)>,
                'parents': <list of parent hashes (list[str])>
            }

        Args:
            commit (Commit): A commit object to be checked for a cherry-pick scenario.
        """
        potential_cherry_pick_match = self._cherry_pick_pattern.search(commit.message)
        if potential_cherry_pick_match:
            self.accumulator['cherry_pick_scenarios'].append({
                'cherry_pick_commit': commit.hexsha,
                'cherry_commit': potential_cherry_pick_match[0],
                'parents': [parent.hexsha for parent in commit.parents]
            })

    def _update_frontier_with(self, commit: Commit, frontier: Queue, is_merge_commit: bool):
        """
        Adds the commit's parents to the frontier and returns the frontier.

        Args:
            commit (Commit): The commit object to update the frontier with.
            frontier (Queue): The queue containing the commits to be processed.
            is_merge_commit (bool): A boolean indicating whether the given commit is a merge commit.

        Returns:
            frontier (Queue): The updated queue containing the commits to be processed.
        """
        if is_merge_commit:
            for parent in commit.parents:
                # Ensure we continue on any path that is left available
                if parent.hexsha not in self.visited_commits:
                    frontier.put(parent)
        elif len(commit.parents) == 1:
            frontier.put(commit.parents[0])

        return frontier

    def _update_commit_message_tracker(self, commit: Commit):
        """
        If a new commit message is detected, adds a new dict element, otherwise appends the commit to the
        list at `commit.message`.

        Args:
            commit (Commit): The commit to update the commit message tracker with.
        """
        if commit.message in self.seen_commit_messages:
            self.seen_commit_messages[commit.message].append(commit)
        else:
            self.seen_commit_messages.update({commit.message: [commit]})

    def _mine_commits_with_duplicate_messages_for_cherry_pick_scenarios(self):
        """
        Mines commits with duplicate messages for cherry pick scenarios.

        If two commits commit messages are identical and so are their patch ids, they are additional cherry-pick scenarios.
        Note that this function early stops after collecting 50 additional scenarios, to avoid excessive compute
        incurred in very large repositories.

        Edge cases:
            - A commit can be present as a cherry for multiple commits in different scenarios, iff it has been picked
                multiple times.
        """
        duplicate_messages = [{k: v} for k, v in self.seen_commit_messages.items() if len(v) > 1]

        if len(duplicate_messages) == 0:
            return []

        additional_cherry_pick_scenarios = []
        start_time = time()
        timeout = 180

        # Start with the messages with the least amount of duplicates (ascending), to cover the most ground
        # before the timeout. This way we ensure a large diversity in the potential samples we
        # consider without spending excessive effort on one message with an excessive amount
        # of duplicates
        duplicate_messages = sorted(duplicate_messages, key=lambda msg: len(list(msg.values())[0]), reverse=False)

        for duplicate_message in tqdm(duplicate_messages,
                                      desc='Mining duplicate commit messages for additional cherry-pick scenarios'):
            commits = next(iter(duplicate_message.values()))
            for i, pivot_commit in enumerate(commits):
                comparison_targets = commits[i + 1:]  # Only process triangular sub-matrix without diagonal
                for comparison_target in comparison_targets:
                    if self._do_patch_ids_match(pivot_commit, comparison_target):
                        self._append_cherry_pick_scenario(additional_cherry_pick_scenarios, comparison_target,
                                                          pivot_commit)

                        # If we found a cherry for this commit, it is a cherry-pick commit.
                        # The other comparison_targets could only lead to duplication iff a cherry has been picked
                        # multiple times. Assume original_commit has been picked to previous_cherry_pick_commit. Then,
                        # original_commit was also picked to other_cherry_pick_commit. All three commits introduce the
                        # same patch and have the same commit message. This means this will lead to duplicate scenarios.
                        # To avoid this, we stop processing comparison_targets, once we have found a cherry for the
                        # commit. This way other_cherry_pick_commit will not be matched with original_commit AND
                        # previous_cherry_pick_commit.
                        break

                    if time() > start_time + timeout:
                        print(f'Early stopping mining for additional cherry-pick scenarios timeout of 3min was hit.\n',
                              file=sys.stderr)
                        break
            # Timeout mechanisms to avoid collecting excessive amounts of scenarios from a single repository
            if len(additional_cherry_pick_scenarios) >= 50:
                print(f'Early stopping mining for additional cherry-pick scenarios, because >=50 were already found.\n',
                      file=sys.stderr)
                break
        print(f'Found {len(additional_cherry_pick_scenarios)} additional cherry pick scenarios.', file=sys.stderr)
        return additional_cherry_pick_scenarios

    def _append_cherry_pick_scenario(self, additional_cherry_pick_scenarios: List[Dict], comparison_target: Commit,
                                     pivot_commit: Commit):
        """
        Appends detected identical commits as cherry_pick scenarios to the additional_cherry_pick_scenarios
        accumulator. The chronologically older commit is set as the 'cherry_commit' and the younger commit as
        'cherry_pick_commit'.

        Args:
            additional_cherry_pick_scenarios (List[Dict]): A list of dictionaries that represent additional
                cherry pick scenarios.
            comparison_target (Commit): The commit that is being compared against.
            pivot_commit (Commit): The commit that is used as the pivot for comparison.

        """
        if pivot_commit.committed_datetime < comparison_target.committed_datetime:
            additional_cherry_pick_scenarios.append({
                'cherry_pick_commit': comparison_target.hexsha,
                'cherry_commit': pivot_commit.hexsha,
                'parents': [parent.hexsha for parent in comparison_target.parents]
            })
        elif pivot_commit.committed_datetime > comparison_target.committed_datetime:
            additional_cherry_pick_scenarios.append({
                'cherry_pick_commit': pivot_commit.hexsha,
                'cherry_commit': comparison_target.hexsha,
                'parents': [parent.hexsha for parent in pivot_commit.parents]
            })

    def _do_patch_ids_match(self, commit1: Commit, commit2: Commit) -> bool:
        """
        Checks if two commits apply the same changes, ie are identical.

        Args:
            commit1: The first commit object to compare.
            commit2: The second commit object to compare.

        Returns:
            bool: True if the patch ids of the two commits match, False otherwise.
        """
        patch_sha1 = self._generate_hash_from_patch(commit1)
        patch_sha2 = self._generate_hash_from_patch(commit2)

        return patch_sha1 == patch_sha2

    def _generate_hash_from_patch(self, commit: Commit) -> str:
        """
        Generates a hash from a commit's patch.

        Args:
            commit (Commit): The commit object for which to generate the hash.

        Returns:
            str: The generated hash as a hexadecimal string.
        """
        diff = commit.diff(other=commit.parents[0] if commit.parents else NULL_TREE, create_patch=True)
        try:
            diff_content = ''.join(d.diff.decode('utf-8') for d in diff)
        except UnicodeDecodeError:
            return ''

        # Normalize the patch
        normalized_diff = re.sub(r'^(index|diff|---|\+\+\+) .*\n', '', diff_content, flags=re.MULTILINE)
        normalized_diff = re.sub(r'^\s*\n', '', normalized_diff, flags=re.MULTILINE)

        return hashlib.sha1(normalized_diff.encode('utf-8')).hexdigest()
