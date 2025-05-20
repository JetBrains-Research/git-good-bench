from enum import Enum


class ScenarioType(Enum):
    FILE_COMMIT_CHAIN_REBASE = 'file_commit_chain_scenario-rebase'
    FILE_COMMIT_CHAIN_CHUNK = 'file_commit_chain_scenario-chunk'
    MERGE = 'merge'
    CHERRY_PICK = 'cherry_pick'
