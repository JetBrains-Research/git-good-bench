import ast
import os
import re
import shutil
import stat
import sys
import traceback
from typing import Iterable
from datetime import datetime, timedelta

import yt.wrapper as yt
from git import Repo, GitCommandError, Commit
from pandas import isna

from src.repository_data_scraper.programming_language import ProgrammingLanguage
from src.repository_data_scraper.repository_data_scraper import RepositoryDataScraper
from src.data_processing_scripts.schemas import RepositoryDataRow, SampleDataRow, SampleDataRowV2, SampleDataRowV3, SampleDataRowV4


def _parse_scenarios_from_raw_string(scenarios: str) -> list:
    return ast.literal_eval(scenarios) if scenarios not in ['None', 'none', 'nan', 'NaN'] and not isna(scenarios) else []

def on_rm_error(func, path, exc_info):
    """
    This method is called by the shutil.rmtree() function when it encounters an error while trying to remove a directory
     or a file. It is used to handle the error and continue with the removal operation.

    Parameters:
    - func: A function object that represents the removal function to be called again for the specific path.
        It should accept a single parameter, which is the path to be removed.
    - path: A string that represents the path of the directory or file that encountered the error.
    - exc_info: Unused by the implementation.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


class RepositoryDataMapper(yt.TypedJob):
    sliding_window_size: int = -1

    def __init__(self, sliding_window_size: int = 3):
        super(RepositoryDataMapper, self).__init__()
        self.sliding_window_size = sliding_window_size
        print(f'Using sliding_window_size={self.sliding_window_size}', file=sys.stderr)

    def __call__(self, row: RepositoryDataRow) -> Iterable[RepositoryDataRow]:
        repository_folder = "__".join(row.name.split("/"))
        path_to_repository = os.path.join('/slot/sandbox/repos', repository_folder)
        try:
            repo_instance = Repo.clone_from(f'https://github.com/{row.name}.git',
                                            f'{path_to_repository}')

            os.chdir(path_to_repository)
            print(os.getcwd(), file=sys.stderr)

            if row.programming_language == 'kotlin':
                programming_language = ProgrammingLanguage.KOTLIN
            elif row.programming_language == 'java':
                programming_language = ProgrammingLanguage.JAVA
            elif row.programming_language == 'python':
                programming_language = ProgrammingLanguage.PYTHON
            else:
                raise ValueError(f'Could not parse programming language: {row.programming_language}'
                                 '. Supported values: "kotlin", "java", "python"')

            repo_scraper = RepositoryDataScraper(repository=repo_instance,
                                                 programming_language=programming_language,
                                                 repository_name=row.name,
                                                 sliding_window_size=self.sliding_window_size)
            repo_scraper.scrape()

            row.file_commit_gram_scenarios = str(repo_scraper.accumulator['file_commit_gram_scenarios'])
            row.merge_scenarios = str(repo_scraper.accumulator['merge_scenarios'])
            row.cherry_pick_scenarios = str(repo_scraper.accumulator['cherry_pick_scenarios'])

            # Move back into tmpfs working directrory
            os.chdir('..')

            print('Current working directory: '+os.getcwd(), file=sys.stderr)
            print(os.listdir('.'), file=sys.stderr)

            shutil.rmtree(repository_folder, onerror=on_rm_error)

            print(os.listdir('.'), file=sys.stderr)
        except Exception as e:
            print(traceback.format_exc(), file=sys.stderr)
            row.error = traceback.format_exc()
            yield row  # Note that the column scrapedData could be empty here
        finally:
            yield row

class ErrorFilteringMapper(yt.TypedJob):

    def __call__(self, row: RepositoryDataRow) -> Iterable[RepositoryDataRow]:
        parsed_cherry_pick_scenarios = _parse_scenarios_from_raw_string(row.cherry_pick_scenarios)

        # No cherry pick scenarios available in this repository
        if not parsed_cherry_pick_scenarios:
            yield row


        parsed_cherry_pick_scenarios = [cherry_pick_scenario for cherry_pick_scenario in parsed_cherry_pick_scenarios \
                                        if len(cherry_pick_scenario['parents']) == 1]

        row.cherry_pick_scenarios = str(parsed_cherry_pick_scenarios)
        if not row.error:
            yield row

def _detect_cherry_pick_conflicts_in(remerge_result):
    return re.findall(r'(CONFLICT (?:.+))', remerge_result)

def _detect_merge_conflicts_in(remerge_result):
    return re.findall(r'(<<<<<<< [a-z0-9 ()]+\n|=======|>>>>>>> [a-z0-9 ()]+\n)', remerge_result)

def _detect_manual_changes_in(remerge_result):
    has_manual_changes = re.findall(r'(diff --git (?:a|b)/\w+\.\w+)', remerge_result)
    return has_manual_changes != []

def process_merge_scenarios(parsed_merge_scenarios, repo_instance):
    merge_scenarios = []
    for merge_scenario in parsed_merge_scenarios:
        try:
            remerge_result = repo_instance.git.show('--remerge-diff', f'{merge_scenario["merge_commit_hash"]}')

            # If the remerge shows any diff output there must have been manual changes
            merge_scenario['has_manual_changes'] = _detect_manual_changes_in(remerge_result)

            # We know the remerge output contains a diff. If this this contains merge conflict markers, there must
            # have been a merge conflict
            merge_scenario['has_conflict'] = (_detect_merge_conflicts_in(remerge_result) != [])

            merge_scenarios.append(merge_scenario)
        except GitCommandError as e:
            if 'unknown revision or path not in the working tree.' in e.stdout:
                print(f'Commit {merge_scenario["merge_commit_hash"]} no longer exists in the repository.'
                      f'This may happen if there is some time between the execution of this mapper '
                      f'and the initial dataset collection', file=sys.stderr)
            else:
                print(f'Other, unexpected error occurred - continuing:\n{e.stdout}', file=sys.stderr)

    print(f'Found {len(merge_scenarios)} merge scenarios.\n', file=sys.stderr)
    return merge_scenarios


def process_cherry_pick_scenarios(parsed_cherry_pick_scenarios, repo_instance):
    cherry_pick_scenarios = []
    for cherry_pick_scenario in parsed_cherry_pick_scenarios:
        repo_instance.git.checkout(f'{cherry_pick_scenario["parents"][0]}')
        repo_instance.git.branch('cherry_pick_isolation_branch')

        try:
            cherry_commit = Commit(repo_instance, bytes.fromhex(cherry_pick_scenario['cherry_commit']))
            if len(cherry_commit.parents) == 1:
                repo_instance.git.cherry_pick(f'{cherry_pick_scenario["cherry_commit"]}')
            else:
                print(f'Cherry commit is a merge with {len(cherry_commit.parents)} parents. '
                      f'It is unclear which side of the merge should be picked, skipping and removing this scenario.\n', file=sys.stderr)
        except ValueError as e:
            print(f'Commit {cherry_pick_scenario["cherry_commit"]} appears to no longer exist in the repository: {str(e)}'
                  f'This may happen if there is some time between the execution of this mapper '
                  f'and the initial dataset collection. Skipping and removing this scenario.', file=sys.stderr)
        except GitCommandError as e:
            print('Caught GitCommandError. Checking if it is the result of a merge conflict.', file=sys.stderr)

            # We know the remerge output contains a diff. If this this contains merge conflict markers, there must
            # have been a merge conflict
            cherry_pick_scenario['has_conflict'] = (_detect_cherry_pick_conflicts_in(e.stdout) != [])

            if cherry_pick_scenario['has_conflict']:
                print(f'Found merge conflict:\n{e}', file=sys.stderr)
                cherry_pick_scenarios.append(cherry_pick_scenario)
                repo_instance.git.cherry_pick('--abort')
                print(f'Aborted cherry-pick: {repo_instance.git.status()}\n', file=sys.stderr)
            elif 'fatal: bad object' in e.stdout:
                print(f'Commit {cherry_pick_scenario["cherry_commit"]} no longer exists in the repository.'
                      f'This may happen if there is some time between the execution of this mapper '
                      f'and the initial dataset collection', file=sys.stderr)
            else:
                print(f'Other, unexpected error occurred:\n{e}', file=sys.stderr)
                print(f'Current scenario:\n{cherry_pick_scenario}', file=sys.stderr)

        repo_instance.git.checkout('-') # Checkout last branch, ie. main
        repo_instance.git.branch('-D', 'cherry_pick_isolation_branch')

    print(f'Found {len(cherry_pick_scenarios)} cherry-pick scenarios.\n', file=sys.stderr)
    return cherry_pick_scenarios


class MergeConflictMapper(yt.TypedJob):
    """
    Mapper that checks whether there has been a conflict for all merge and cherry-pick scenario's.

    Performs the following actions:
        Renames the flag 'had_conflicts' to 'has_conflict' for merge scenarios and adds this flag to cherry-pick scenarios.
        Adds the 'has_manual_changes' flag to merge scenarios for which no conflict occurred, but which were edited during the merge.
        'has_conflict' is now correctly be set to True ONLY for scenarios for which a conflict actually occurred.
    """

    def __call__(self, row: RepositoryDataRow) -> Iterable[RepositoryDataRow]:
        parsed_merge_scenarios = _parse_scenarios_from_raw_string(row.merge_scenarios)
        parsed_cherry_pick_scenarios = _parse_scenarios_from_raw_string(row.cherry_pick_scenarios)

        # No scenarios available in this repository
        if not parsed_merge_scenarios and not parsed_cherry_pick_scenarios:
            yield row

        # Setup repository if since is data to be processed
        repository_folder = "__".join(row.name.split("/"))
        path_to_repository = os.path.join('/slot/sandbox/repos', repository_folder)
        try:
            repo_instance = Repo.clone_from(f'https://github.com/{row.name}.git',
                                            f'{path_to_repository}')
            repo_instance.git.fetch('--all')

            os.chdir(path_to_repository)

            if parsed_merge_scenarios:
                print(f'Processing merge scenarios in {row.name}.', file=sys.stderr)
                merge_scenarios = process_merge_scenarios(parsed_merge_scenarios, repo_instance)
                row.merge_scenarios = str(merge_scenarios)

            if parsed_cherry_pick_scenarios:
                print(f'Processing cherry-pick scenarios in {row.name}.', file=sys.stderr)
                cherry_pick_scenarios = process_cherry_pick_scenarios(parsed_cherry_pick_scenarios, repo_instance)
                row.cherry_pick_scenarios = str(cherry_pick_scenarios)

            os.chdir('..')
            shutil.rmtree(repository_folder, onerror=on_rm_error)
        except Exception:
            print(traceback.format_exc(), file=sys.stderr)
            row.error = traceback.format_exc()
        finally:
            yield row


class SelectOnlyMergeScenariosWithConflictsMapper(yt.TypedJob):
    """
        Removes the deprecated 'had_conflicts' field and filters to scenarios where has_conflicts == True.
        """

    def __call__(self, row: RepositoryDataRow) -> Iterable[RepositoryDataRow]:
        parsed_merge_scenarios = _parse_scenarios_from_raw_string(row.merge_scenarios)

        if parsed_merge_scenarios:
            merge_scenarios_with_conflicts = []
            for merge_scenario in parsed_merge_scenarios:

                if 'has_conflict' in merge_scenario and merge_scenario['has_conflict']:
                    del merge_scenario['had_conflicts']
                    merge_scenarios_with_conflicts.append(merge_scenario)
            row.merge_scenarios = str(merge_scenarios_with_conflicts)

        yield row


class RemoveFileCommitGramScenariosWithMergesMapper(yt.TypedJob):

    def __call__(self, row: RepositoryDataRow) -> Iterable[RepositoryDataRow]:
        parsed_file_commit_gram_scenarios = _parse_scenarios_from_raw_string(row.file_commit_gram_scenarios)
        parsed_merge_scenarios = _parse_scenarios_from_raw_string(row.merge_scenarios)
        parsed_cherry_pick_scenarios = _parse_scenarios_from_raw_string(row.cherry_pick_scenarios)

        # Setup repository if since is data to be processed
        repository_folder = "__".join(row.name.split("/"))
        path_to_repository = os.path.join('/slot/sandbox/repos', repository_folder)
        try:
            repo_instance = Repo.clone_from(f'https://github.com/{row.name}.git',
                                            f'{path_to_repository}')
            repo_instance.git.fetch('--all')

            os.chdir(path_to_repository)

            # Remove unused indicators from dataset. We will only include scenario that have conflicts for
            # merge and cherry-pick scenarios
            if parsed_merge_scenarios:
                merge_scenarios = []
                for merge_scenario in parsed_merge_scenarios:
                    if 'has_conflict' in merge_scenario:
                        del merge_scenario['has_conflict']
                    if 'has_manual_changes' in merge_scenario:
                        del merge_scenario['has_manual_changes']
                    merge_scenarios.append(merge_scenario)
                row.merge_scenarios = str(merge_scenarios)

            if parsed_cherry_pick_scenarios:
                cherry_pick_scenarios = []
                for cherry_pick_scenario in parsed_cherry_pick_scenarios:
                    if 'has_conflict' in cherry_pick_scenario:
                        del cherry_pick_scenario['has_conflict']
                    cherry_pick_scenarios.append(cherry_pick_scenario)

                row.cherry_pick_scenarios = str(cherry_pick_scenarios)

            if parsed_file_commit_gram_scenarios:
                scenarios_without_merges = []
                for file_commit_gram_scenario in parsed_file_commit_gram_scenarios:
                    try:
                        commit = Commit(repo_instance, bytes.fromhex(file_commit_gram_scenario["first_commit"]))

                        has_merge_commit = False
                        i = 0
                        while i < file_commit_gram_scenario['times_seen_consecutively']:
                            if len(commit.parents) > 1:
                                print(f'Found merge in chain. Repository {row.name}, Commit-{i} {commit}, Parents {commit.parents}',
                                      file=sys.stderr)
                                file_commit_gram_scenario['has_merge_commit'] = True
                                has_merge_commit = True
                                break
                            commit = commit.parents[0]
                            i += 1

                        if not has_merge_commit:
                            scenarios_without_merges.append(file_commit_gram_scenario)
                    except ValueError as e:
                        print(f'Commit {file_commit_gram_scenario["first_commit"]} or a parent appear to no longer exist in the repository.'
                              f'Commit {commit}: {str(e)}',
                              file=sys.stderr)
                        continue
                    except GitCommandError as e:
                        print(f'Other, unexpected error occurred:\n{e}', file=sys.stderr)
                        continue
                    except IndexError as e:
                        print(f'Error shifting to next commit in chain. Commit parents: {commit.parents}, Error: {e}', file=sys.stderr)

                row.file_commit_gram_scenarios = str(scenarios_without_merges)

            os.chdir('..')
            shutil.rmtree(repository_folder, onerror=on_rm_error)
        except Exception:
            print(traceback.format_exc(), file=sys.stderr)
            row.error = traceback.format_exc()
        finally:
            yield row

class SelectMergeScenariosWithExactlyTwoParents(yt.TypedJob):
    """
        Only retains scenarios with exactly two parent commits for the merge commit.
    """

    def __call__(self, row: RepositoryDataRow) -> Iterable[RepositoryDataRow]:
        parsed_merge_scenarios = _parse_scenarios_from_raw_string(row.merge_scenarios)

        if parsed_merge_scenarios:
            parsed_merge_scenarios = [merge_scenario for merge_scenario in
                                            parsed_merge_scenarios \
                                            if len(merge_scenario['parents']) == 2]

            row.merge_scenarios = str(parsed_merge_scenarios)

        yield row

class ImproveMergeConflictScenarioQualityMapper(yt.TypedJob):
    """
    Mapper that removes merge conflicts where a conflict occurred in a non-Java, non-Python or non-Kotlin file.
    Furthermore, it introduces and populates new metadata fields for merge scenarios:
        - total_number_of_merge_conflicts: The total number of merge conflicts across all files in the merge commit.
        - files_in_merge_conflict: A list of files in the merge commit that have merge conflicts.
        - number_of_files_with_merge_conflict: The number of files in the merge commit that have merge conflicts.
    """

    def __call__(self, row: RepositoryDataRow) -> Iterable[RepositoryDataRow]:
        parsed_merge_scenarios = _parse_scenarios_from_raw_string(row.merge_scenarios)
        parsed_cherry_pick_scenarios = _parse_scenarios_from_raw_string(row.cherry_pick_scenarios)

        # No scenarios available in this repository
        if not parsed_merge_scenarios and not parsed_cherry_pick_scenarios:
            yield row

        # Setup repository if since is data to be processed
        repository_folder = "__".join(row.name.split("/"))
        path_to_repository = os.path.join('/slot/sandbox/repos', repository_folder)
        try:
            repo_instance = Repo.clone_from(f'https://github.com/{row.name}.git',
                                            f'{path_to_repository}')
            repo_instance.git.fetch('--all')

            os.chdir(path_to_repository)

            if parsed_merge_scenarios:
                print(f'Processing merge scenarios in {row.name}.', file=sys.stderr)
                merge_scenarios = []
                for merge_scenario in parsed_merge_scenarios:
                    try:
                        remerge_result = repo_instance.git.show('--remerge-diff',
                                                                f'{merge_scenario["merge_commit_hash"]}')

                        # We know the remerge output contains a diff. If this contains merge conflict markers, there must
                        # have been a merge conflictgit
                        remerge_result_per_file = remerge_result.split('diff --git')[
                                                  1:]  # remove remerge commit header and metadata
                        if any([_does_line_contain_non_programming_language_files(r.splitlines()[0]) for r in
                                remerge_result_per_file]):
                            print(f'Merge conflict in non-PL file. Skipping and removing this scenario.\n', file=sys.stderr)
                            print('\n'.join([r.splitlines()[0] for r in remerge_result_per_file]), file=sys.stderr)
                            continue

                        diffs_with_conflicts = [diff for diff in remerge_result_per_file if '>>>>>>>' in diff]
                        total_number_of_conflicts = 0
                        files_with_conflicts = []
                        for diff in diffs_with_conflicts:
                            files_with_conflicts.append(diff.split(' b')[0][3:])
                            total_number_of_conflicts += len(re.findall(r'<<<<<<<', diff))

                        merge_scenario['number_of_files_with_merge_conflict'] = len(files_with_conflicts)
                        merge_scenario['total_number_of_merge_conflicts'] = total_number_of_conflicts
                        merge_scenario['files_in_merge_conflict'] = files_with_conflicts

                        print(f"\n##### MERGE #######\nDetected {merge_scenario['total_number_of_merge_conflicts']} merge conflicts (merge) in "
                              f"{merge_scenario['number_of_files_with_merge_conflict']} files. Files: {merge_scenario['files_in_merge_conflict']}", file=sys.stderr)

                        if total_number_of_conflicts > 0:
                            merge_scenarios.append(merge_scenario)
                    except GitCommandError as e:
                        if 'unknown revision or path not in the working tree.' in e.stdout:
                            print(f'Commit {merge_scenario["merge_commit_hash"]} no longer exists in the repository.'
                                  f'This may happen if there is some time between the execution of this mapper '
                                  f'and the initial dataset collection', file=sys.stderr)
                        else:
                            print(f'Other, unexpected error occurred - continuing:\n{e.stdout}', file=sys.stderr)

                print(f'Found {len(merge_scenarios)} merge scenarios.\n', file=sys.stderr)
                row.merge_scenarios = str(merge_scenarios)

            if parsed_cherry_pick_scenarios:
                print(f'Processing cherry-pick scenarios in {row.name}.', file=sys.stderr)
                cherry_pick_scenarios = []
                for cherry_pick_scenario in parsed_cherry_pick_scenarios:
                    repo_instance.git.checkout(f'{cherry_pick_scenario["parents"][0]}')
                    repo_instance.git.branch('cherry_pick_isolation_branch')

                    try:
                        cherry_commit = Commit(repo_instance, bytes.fromhex(cherry_pick_scenario['cherry_commit']))
                        if len(cherry_commit.parents) == 1:
                            repo_instance.git.cherry_pick(f'{cherry_pick_scenario["cherry_commit"]}')
                        else:
                            print(f'Cherry commit is a merge with {len(cherry_commit.parents)} parents. '
                                  f'It is unclear which side of the merge should be picked, skipping and removing this scenario.\n',
                                  file=sys.stderr)
                    except ValueError as e:
                        print(
                            f'Commit {cherry_pick_scenario["cherry_commit"]} appears to no longer exist in the repository: {str(e)}'
                            f'This may happen if there is some time between the execution of this mapper '
                            f'and the initial dataset collection. Skipping and removing this scenario.',
                            file=sys.stderr)
                    except GitCommandError as e:
                        print('Caught GitCommandError. Checking if it is the result of a merge conflict.',
                              file=sys.stderr)

                        scenario_contains_non_pl_file = False
                        git_error_was_conflict = False
                        cherry_pick_files_with_conflicts = []
                        cherry_pick_total_number_of_conflicts = 0
                        for line in e.stdout.splitlines():
                            if 'CONFLICT' in line:
                                if line.split('.')[-1] not in ['py', 'java', 'kt']:
                                    print(f'\n\n--CHERRY-PICK--\nMerge conflict with unsupported non-programming-language file. Skipping and removing this scenario.\n{e.stdout}', file=sys.stderr)
                                    scenario_contains_non_pl_file = True
                                    break
                                else:
                                    git_error_was_conflict = True
                                    file = re.search(r'(?<= )(?:\w+\/)*\w+\.(?:py|kt|java)', line)
                                    if file:
                                        cherry_pick_files_with_conflicts.append(file.group(0))

                                        with open(file.group(0), 'r') as f:
                                            file_content = f.read()
                                            cherry_pick_total_number_of_conflicts += len(re.findall(r'<<<<<<<', file_content))

                        if scenario_contains_non_pl_file or not git_error_was_conflict:
                            repo_instance.git.cherry_pick('--abort')
                            repo_instance.git.checkout('-')  # Checkout last branch, ie. main
                            repo_instance.git.branch('-D', 'cherry_pick_isolation_branch')
                            continue

                        cherry_pick_scenario['number_of_files_with_merge_conflict'] = len(cherry_pick_files_with_conflicts)
                        cherry_pick_scenario['total_number_of_merge_conflicts'] = cherry_pick_total_number_of_conflicts
                        cherry_pick_scenario['files_in_merge_conflict'] = cherry_pick_files_with_conflicts

                        print(
                            f"\n###### CHERRY-PICK ######\nDetected {cherry_pick_scenario['total_number_of_merge_conflicts']} merge conflicts (cherry-pick) in "
                            f"{cherry_pick_scenario['number_of_files_with_merge_conflict']} files. Files: {cherry_pick_scenario['files_in_merge_conflict']}",
                            file=sys.stderr)

                        cherry_pick_scenarios.append(cherry_pick_scenario)

                        repo_instance.git.cherry_pick('--abort')
                        print(f'Aborted cherry-pick: {repo_instance.git.status()}\n', file=sys.stderr)
                        if 'fatal: bad object' in e.stdout:
                            print(f'Commit {cherry_pick_scenario["cherry_commit"]} no longer exists in the repository.'
                                  f'This may happen if there is some time between the execution of this mapper '
                                  f'and the initial dataset collection', file=sys.stderr)
                        else:
                            print(f'Other, unexpected error occurred:\n{e}', file=sys.stderr)
                            print(f'Current scenario:\n{cherry_pick_scenario}', file=sys.stderr)

                    repo_instance.git.checkout('-')  # Checkout last branch, ie. main
                    repo_instance.git.branch('-D', 'cherry_pick_isolation_branch')

                print(f'Found {len(cherry_pick_scenarios)} cherry-pick scenarios.\n', file=sys.stderr)
                row.cherry_pick_scenarios = str(cherry_pick_scenarios)

            os.chdir('..')
            shutil.rmtree(repository_folder, onerror=on_rm_error)
        except Exception:
            print(traceback.format_exc(), file=sys.stderr)
            row.error = traceback.format_exc()
        finally:
            yield row

def _does_line_contain_non_programming_language_files(line: str) -> bool:
    return not (line.endswith('.java') or line.endswith('.py') or line.endswith('.kt'))

class DetermineFileCommitGramPurityMapper(yt.TypedJob):
    """
    Mapper that determines the amount of other files present in a file commit gram scenario and the relative
    amount of changes that were made in the file the scenario concerns itself with compared to the overall changes.
    A change is interpreted as a line with a prefix + or - in the diff.

    Should operate on the finished dataset and benchmark tables.
    """

    def __call__(self, row: SampleDataRowV4) -> Iterable[SampleDataRowV4]:
        # Skip if not a file commit chain scenario
        if row.sample_type != 'file_commit_chain':
            yield row
            return

        # Parse the scenario from the row
        scenario = ast.literal_eval(row.scenario)

        # Setup repository if since is data to be processed
        repository_folder = "__".join(row.name.split("/"))
        path_to_repository = os.path.join('/slot/sandbox/repos', repository_folder)
        try:
            repo_instance = Repo.clone_from(f'https://github.com/{row.name}.git',
                                            f'{path_to_repository}')
            repo_instance.git.fetch('--all')

            os.chdir(path_to_repository)

            print(f'Processing file-commit gram scenario in {row.name}.', file=sys.stderr)
            try:
                # First, get the list of files that were changed across all commits
                repo_instance.git.checkout(f'{scenario["newest_commit"]}')
                commits = repo_instance.git.log(format='%H', n=f'{scenario["times_seen_consecutively"]}')
                commits = commits.strip().split('\n')

                # Get all files that were changed in any of the commits
                all_changed_files = set()
                for commit in commits:
                    # Get list of files changed in this commit
                    changed_files = repo_instance.git.show('--pretty=format:', '--name-only', commit)
                    for line in changed_files.strip().split('\n'):
                        if line:  # Skip empty lines
                            file_path = line
                            all_changed_files.add(file_path)

                # Now reset to the state before the commits and get staged files
                repo_instance.git.reset(f'HEAD~{scenario["times_seen_consecutively"]}')
                staged_files = set()
                status_output = repo_instance.git.status('--porcelain')
                for line in status_output.strip().split('\n'):
                    if line:  # Skip empty lines
                        # Extract just the filename, ignoring the status (git status --porcelain format is XY PATH)
                        file_path = line.strip().split(' ')[-1]
                        staged_files.add(file_path)

                # Files that were changed but not in staging area have changes that cancel out
                files_with_cancelled_changes = all_changed_files - staged_files

                repo_instance.git.checkout('-f', f'{scenario["newest_commit"]}')

                changes_in_file = 0
                total_changes = 0
                contains_non_programming_language_file = False
                offending_line = None
                for commit in commits:
                    commit_diff = repo_instance.git.show(f'{commit}')

                    lines = commit_diff.split('\n')
                    in_target_file_diff = False
                    in_cancelled_file_diff = False
                    have_encountered_first_diff = False

                    for line in lines:
                        # Skip header until first diff
                        if not line.startswith('diff --git') and not have_encountered_first_diff:
                            continue

                        # Start of a new diff section
                        if line.startswith('diff --git'):
                            in_target_file_diff = False
                            in_cancelled_file_diff = False
                            have_encountered_first_diff = True

                            # Extract the file path from the diff line (format: diff --git a/PATH b/PATH)
                            file_path = line.split(' b/')[-1]
                            
                            # Skip files that have cancelled changes
                            if file_path in files_with_cancelled_changes:
                                in_cancelled_file_diff = True
                                continue

                            if f'diff --git a/{scenario["file"]} b/{scenario["file"]}' in line:
                                in_target_file_diff = True
                            else:
                                file_extension = re.search(r'(?:diff --git a\/.*)(\.(?:\w+))(?= )\b', line)
                                contains_non_programming_language_file = file_extension and file_extension.group(1) not in ['.java', '.py', '.kt']
                                if contains_non_programming_language_file:
                                    offending_line = line
                                    break
                        # Ensure we only count changes and not metadata change information and also separate
                        # counting of target and noise files
                        elif not in_cancelled_file_diff:  # Only count changes if not in a cancelled file
                            if in_target_file_diff and (line.startswith('+') or line.startswith('-')) and not (
                                    line.startswith('---') or line.startswith('+++')):
                                changes_in_file += 1
                                total_changes += 1
                            elif (line.startswith('+') or line.startswith('-')) and not (
                                    line.startswith('---') or line.startswith('+++')):
                                total_changes += 1

                    if contains_non_programming_language_file:
                        break

                if contains_non_programming_language_file:
                    print(f'Skipping and removing file-commit gram scenario due to non-PL file in {offending_line}.', file=sys.stderr)
                    return  # skip and remove scenario
                elif total_changes > 0:
                    scenario['purity'] = round(changes_in_file / total_changes, 2)
                else:
                    scenario['purity'] = 0

                print(f"\n##### FILE-COMMIT GRAM #######\nDetected scenario with purity {scenario['purity']}.", file=sys.stderr)

                row.scenario = str(scenario)
                yield row

            except GitCommandError as e:
                if 'unknown revision or path not in the working tree.' in e.stdout:
                    print(f'A commit in this file-commit gram scenario no longer exists in the repository.'
                          f'This may happen if there is some time between the execution of this mapper '
                          f'and the initial dataset collection', file=sys.stderr)
                else:
                    print(f'Other, unexpected error occurred - continuing:\n{e}', file=sys.stderr)

        except Exception:
            print(traceback.format_exc(), file=sys.stderr)
            row.error = traceback.format_exc()
        finally:
            yield row

class RemoveArchivedReposMapper(yt.TypedJob):

    def __call__(self, row: RepositoryDataRow) -> Iterable[RepositoryDataRow]:
        if not row.is_archived:
            yield row


class TransformDatasetToOneRowPerSample(yt.TypedJob):
    """
    Transforms the dataset from a table with one row per repository to a table with one row per sample.
    More specifically, this mapper transforms from the RepositoryDataRow schema to the SampleDataRow schema.

    We remove some metadata fields that we deemed noisy and unnecessary for the publication. Furthermore,
    we extract the scenario type and the scenario into separate fields and introduce project_size and
    project_activity fields which we will use for down sampling the dataset (ie as a quality criterion).
    """

    def _calculate_project_size(self, code_lines: float) -> str:
        """
        Calculate project size category based on the number of code lines.

        Args:
            code_lines: Number of lines of code in the project

        Returns:
            str: Size category (tiny, small, medium, large, or huge)
        """
        if code_lines < 1000:
            return 'tiny'
        elif code_lines < 10000:
            return 'small'
        elif code_lines < 100000:
            return 'medium'
        elif code_lines < 1000000:
            return 'large'
        else:
            return 'huge'

    def _calculate_project_activity(self, last_commit: str) -> str:
        """
        Calculate project activity based on the time difference between last_commit and May 31, 2024 23:59.

        Args:
            last_commit: Timestamp string of the last commit

        Returns:
            str: Activity category (day, week, month, quarter, year, two years, or older than two years)
        """
        if not last_commit or isna(last_commit):
            return 'older than two years'

        try:
            last_commit_date = datetime.fromisoformat(last_commit.replace('Z', '+00:00'))
            target_date = datetime(2024, 5, 31, 23, 59, tzinfo=last_commit_date.tzinfo)
            time_diff = target_date - last_commit_date

            if time_diff <= timedelta(days=1):
                return 'day'
            elif time_diff <= timedelta(days=7):
                return 'week'
            elif time_diff <= timedelta(days=30):
                return 'month'
            elif time_diff <= timedelta(days=90):
                return 'quarter'
            elif time_diff <= timedelta(days=365):
                return 'year'
            elif time_diff <= timedelta(days=730):
                return 'two years'
            else:
                return 'older than two years'
        except (ValueError, TypeError):
            return 'older than two years'

    def __call__(self, row: RepositoryDataRow) -> Iterable[SampleDataRow]:
        if not row.size > 4000000:
            # Parse all scenarios
            parsed_file_commit_gram_scenarios = _parse_scenarios_from_raw_string(row.file_commit_gram_scenarios)
            parsed_merge_scenarios = _parse_scenarios_from_raw_string(row.merge_scenarios)
            parsed_cherry_pick_scenarios = _parse_scenarios_from_raw_string(row.cherry_pick_scenarios)

            # Calculate project metrics once
            project_size = self._calculate_project_size(row.code_lines)
            project_activity = self._calculate_project_activity(row.last_commit)

            # Handle file commit gram scenarios
            for scenario_idx, scenario in enumerate(parsed_file_commit_gram_scenarios):
                yield SampleDataRow(
                    id=f'{row.name}-file_commit_gram-{scenario_idx:05d}',
                    name=row.name,
                    commits=row.commits,
                    branches=row.branches,
                    releases=row.releases,
                    forks=row.forks,
                    default_branch=row.default_branch,
                    license=row.license,
                    watchers=row.watchers,
                    stargazers=row.stargazers,
                    contributors=row.contributors,
                    created_at=row.created_at,
                    blank_lines=row.blank_lines,
                    code_lines=row.code_lines,
                    comment_lines=row.comment_lines,
                    last_commit=row.last_commit,
                    topics=row.topics,
                    programming_language=row.programming_language,
                    scenario=str(scenario),
                    scenario_type='file_commit_gram',
                    project_size=project_size,
                    project_activity=project_activity
                )

            # Handle merge scenarios
            for scenario_idx, scenario in enumerate(parsed_merge_scenarios):
                yield SampleDataRow(
                    id=f'{row.name}-merge-{scenario_idx:05d}',
                    name=row.name,
                    commits=row.commits,
                    branches=row.branches,
                    releases=row.releases,
                    forks=row.forks,
                    default_branch=row.default_branch,
                    license=row.license,
                    watchers=row.watchers,
                    stargazers=row.stargazers,
                    contributors=row.contributors,
                    created_at=row.created_at,
                    blank_lines=row.blank_lines,
                    code_lines=row.code_lines,
                    comment_lines=row.comment_lines,
                    last_commit=row.last_commit,
                    topics=row.topics,
                    programming_language=row.programming_language,
                    scenario=str(scenario),
                    scenario_type='merge',
                    project_size=project_size,
                    project_activity=project_activity
                )

            # Handle cherry-pick scenarios
            for scenario_idx, scenario in enumerate(parsed_cherry_pick_scenarios):
                yield SampleDataRow(
                    id=f'{row.name}-cherry_pick-{scenario_idx:05d}',
                    name=row.name,
                    commits=row.commits,
                    branches=row.branches,
                    releases=row.releases,
                    forks=row.forks,
                    default_branch=row.default_branch,
                    license=row.license,
                    watchers=row.watchers,
                    stargazers=row.stargazers,
                    contributors=row.contributors,
                    created_at=row.created_at,
                    blank_lines=row.blank_lines,
                    code_lines=row.code_lines,
                    comment_lines=row.comment_lines,
                    last_commit=row.last_commit,
                    topics=row.topics,
                    programming_language=row.programming_language,
                    scenario=str(scenario),
                    scenario_type='cherry_pick',
                    project_size=project_size,
                    project_activity=project_activity
                )

class RefineDatasetCoarse(yt.TypedJob):
    """
    Apply heuristic-based filters to downsample the dataset in a first step. We will then use the resulting dataset
    as a basis for down sampling to the Lite and Regular versions. In this step we just exclude samples that we dont
    want to use in any case.

    - Remove cherry-pick scenario due to high churn
    - Remove repositories with < 1000 stars as quality proxy
    - Remove file commit gram scenarios with < 0.5 purity as they are too noisy wrt the file they are about
    - Remove merge scenarios with 0 total_number_of_merge_conflicts, they slipped through due to an initialization bug in
        ImproveMergeConflictScenarioQualityMapper that is now fixed
    - Remove stale repositories (no commits in last months)
    - Introduce an upper boundary for total_number_of_merge_conflicts and times_seen_consecutively based on their 90th quantile
        this ensures that the agent will have a realistic change to solve the scenarios in the amount of turns it has.
    """

    def __call__(self, row: SampleDataRow) -> Iterable[SampleDataRow]:
        if not row.scenario_type == 'cherry_pick' and row.stargazers >= 1000 and row.project_activity in ['day', 'week', 'month']:
            scenario = ast.literal_eval(row.scenario)

            if (row.scenario_type == 'merge' and scenario['total_number_of_merge_conflicts'] != 0 and scenario['total_number_of_merge_conflicts'] <= 8) or \
                (row.scenario_type == 'file_commit_gram' and scenario['purity'] >= 0.5 and scenario['times_seen_consecutively'] <= 6):
                yield row


class RemoveFileCommitGramScenariosWithAddedFile(yt.TypedJob):
    """
    Remove file-commit gram scenarios that concern a file that was added in the scenario. Introduce difficulty field.

    If the file was created in the chronologically first commit, then we will just get a diff with one hunk that is the
    entire file. Our approach cannot meaningfully improve the git history in this case, since we ask the LLM to select
    hunks. Thus we remove these samples.
    """

    def _compute_file_commit_gram_difficulty(self, scenario) -> str | None:
        if scenario['purity'] == 1:
            return 'easy'
        elif 1 > scenario['purity'] >= 0.75:
            return 'medium'
        elif 0.75 > scenario['purity'] >= 0.5:
            return 'hard'

    def _compute_merge_conflict_difficulty(self, scenario) -> str | None:
        if scenario['number_of_files_with_merge_conflict'] == 1 and scenario['total_number_of_merge_conflicts'] == 1:
            return 'easy'
        elif scenario['number_of_files_with_merge_conflict'] == 1 and scenario['total_number_of_merge_conflicts'] > 1:
            return 'medium'
        elif scenario['number_of_files_with_merge_conflict'] > 1 and scenario['total_number_of_merge_conflicts'] > 1:
            return 'hard'

    def __call__(self, row: SampleDataRow) -> Iterable[SampleDataRowV2]:
        scenario = ast.literal_eval(row.scenario)
        if row.scenario_type == 'file_commit_gram':
            # Setup repository if since is data to be processed
            repository_folder = "__".join(row.name.split("/"))
            path_to_repository = os.path.join('/slot/sandbox/repos', repository_folder)
            try:
                repo_instance = Repo.clone_from(f'https://github.com/{row.name}.git',
                                                f'{path_to_repository}')
                repo_instance.git.fetch('--all')
                os.chdir(path_to_repository)

                try:
                    show_output = repo_instance.git.show(f'{scenario["last_commit"]}',
                                                         '--pretty=format:"%h - %an, %ar : %s"', '--name-status')
                    lines = show_output.splitlines()
                    print(lines, file=sys.stderr)
                    line_with_file = [l for l in lines if scenario['file'] in l]
                    print(line_with_file, file=sys.stderr)
                    print(line_with_file[0], file=sys.stderr)
                    if not line_with_file[0].strip().startswith('A'):
                        print('KEEPING scenario, because the file it concerns itself with is MODIFIED\n\n',
                              file=sys.stderr)
                        row.scenario = str(scenario)
                        yield SampleDataRowV2(row, self._compute_file_commit_gram_difficulty(scenario))
                    else:
                        print('SKIPPING scenario, because the file it concerns itself with is ADDED\n\n', file=sys.stderr)
                except ValueError as e:
                    print(
                        f'Commit {scenario["last_commit"]} appears to no longer exist in the repository: {str(e)}',
                        file=sys.stderr)
                except GitCommandError as e:
                    print(f'Other, unexpected error occurred:\n{e}', file=sys.stderr)

            except Exception:
                print(traceback.format_exc(), file=sys.stderr)
            finally:
                os.chdir('..')
                shutil.rmtree(repository_folder, onerror=on_rm_error)
        elif row.scenario_type == 'merge':
            yield SampleDataRowV2(row, self._compute_merge_conflict_difficulty(scenario))


class ClarifyDatasetMapper(yt.TypedJob):
    """
    Rename scenario_type to sample_type, file_commit_gram to file_commit_chain and first_commit and last_commit for
    file commit grams (in the scenario field) to newest_commit and oldest_commit.

    Should operate on the git_good_bench tables.
    """


    def __call__(self, row: SampleDataRowV2) -> Iterable[SampleDataRowV3]:
        scenario = ast.literal_eval(row.scenario)
        if row.scenario_type == 'file_commit_gram':
            row.scenario_type = 'file_commit_chain'

            id_components = row.id.split('-')
            row.id = id_components[0] + '-' + row.scenario_type + '-' + id_components[-1]

            scenario['newest_commit'] = scenario['first_commit']
            del scenario['first_commit']
            scenario['oldest_commit'] = scenario['last_commit']
            del scenario['last_commit']

            row.scenario = str(scenario)

            yield SampleDataRowV3(row)
        elif row.scenario_type == 'merge':
            yield SampleDataRowV3(row)

class RemoveUnneededMetadataMapper(yt.TypedJob):
    """
    Removes code_lines, comment_lines, blank_lines, last_commit, watchers, commits, branches, releases, forks, contributors

    Should operate on the git_good_bench tables.
    """


    def __call__(self, row: SampleDataRowV3) -> Iterable[SampleDataRowV4]:
        yield SampleDataRowV4(row)

class CheckIfFileCommitChainsContainNonPLFiles(yt.TypedJob):
    """
    Read only mapper to check if commits in file-commit chains may contain files other than Python, Java, or Kotlin files.
    """

    def __call__(self, row: SampleDataRowV4) -> Iterable[SampleDataRowV4]:
        scenario = ast.literal_eval(row.scenario)
        if row.sample_type == 'file_commit_chain':
            repository_folder = "__".join(row.name.split("/"))
            path_to_repository = os.path.join('/slot/sandbox/repos', repository_folder)
            try:
                repo_instance = Repo.clone_from(f'https://github.com/{row.name}.git',
                                                f'{path_to_repository}')
                repo_instance.git.fetch('--all')
                os.chdir(path_to_repository)

                try:
                    # First checkout the newest commit
                    try:
                        repo_instance.git.checkout(f'{scenario["newest_commit"]}')
                    except GitCommandError as e:
                        print(f'Failed to checkout newest commit {scenario["newest_commit"]}: {e}', file=sys.stderr)
                        yield row

                    commits = repo_instance.git.log(format='%H', n=f'{scenario["times_seen_consecutively"]}').strip().split('\n')

                    # Check each commit for non-PL files
                    contains_non_pl_files = False
                    non_pl_files = []
                    for commit in commits:
                        try:
                            show_output = repo_instance.git.show(commit,
                                                             '--pretty=format:"%h - %an, %ar : %s"', '--name-status')
                        except GitCommandError as e:
                            print(f'Failed to process commit {commit}: {e}', file=sys.stderr)
                            continue

                        lines = show_output.splitlines()[1:]  # Skip the first line which contains commit info

                        # Filter out files that are not .py, .java, or .kt
                        for line in lines:
                            # Skip empty lines
                            if not line.strip():
                                continue
                            # Get the file path (last part of the line after status character)
                            file_path = line.strip().split()[-1]

                            # Check if file has an extension and if it's not a PL file
                            if '.' in file_path:
                                extension = file_path.split('.')[-1]
                                if extension not in ['py', 'java', 'kt']:
                                    non_pl_files.append(file_path)
                            else:
                                # Files without extension are considered non-PL files
                                non_pl_files.append(file_path)

                    if non_pl_files:
                        contains_non_pl_files = True
                        print(non_pl_files, file=sys.stderr)

                    scenario['contains_non_pl_files'] = contains_non_pl_files
                    if contains_non_pl_files:
                        scenario['non_pl_files'] = non_pl_files
                    row.scenario = str(scenario)
                except ValueError as e:
                    print(
                        f'Commit {scenario["newest_commit"]} appears to no longer exist in the repository: {str(e)}',
                        file=sys.stderr)
                except GitCommandError as e:
                    print(f'Other, unexpected error occurred:\n{e}', file=sys.stderr)

            except Exception:
                print(traceback.format_exc(), file=sys.stderr)
            finally:
                os.chdir('..')
                shutil.rmtree(repository_folder, onerror=on_rm_error)

                if scenario['file'].split('.')[-1] in ['py', 'java', 'kt']:
                    yield row

        elif row.sample_type == 'merge':
            yield row
