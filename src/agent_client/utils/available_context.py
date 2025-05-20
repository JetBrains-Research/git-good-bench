from enum import Enum


class AvailableContext(Enum):
    GIT_STATUS = 'git_status'
    GIT_DIFF = 'git_diff'
    REMAINING_HUNKS = 'remaining_hunks'
    PROGRAMMING_LANGUAGE = 'programming_language'
    REBASE_PARTICIPATING_COMMITS = 'rebase_participating_commits'
    COMMIT_TEMPORAL_ORDERING = 'commit_temporal_ordering'
    COMMIT_TYPE = 'commit_type'
    TOTAL_AMOUNT_OF_MERGE_CONFLICTS = 'total_amount_of_merge_conflicts'
    FILES_WITH_CONFLICTS = 'files_with_conflicts'
    ALL_MERGE_CONFLICTS = 'all_merge_conflicts'
