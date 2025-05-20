from yt.wrapper import yt_dataclass
from typing import Optional
from dataclasses import dataclass


@yt_dataclass
class DummyRow:
    content: str


@yt_dataclass
@dataclass
class RepositoryDataRow:
    id: int
    name: Optional[str]
    is_fork: Optional[bool]
    commits: Optional[int]
    branches: Optional[int]
    releases: Optional[int]
    forks: Optional[int]
    main_language: Optional[str]
    default_branch: Optional[str]
    license: Optional[str]
    homepage: Optional[str]
    watchers: Optional[int]
    stargazers: Optional[int]
    contributors: Optional[int]
    size: Optional[int]
    created_at: Optional[str]
    pushed_at: Optional[str]
    updated_at: Optional[str]
    total_issues: Optional[float]
    open_issues: Optional[float]
    total_pull_requests: Optional[float]
    open_pull_requests: Optional[float]
    blank_lines: Optional[float]
    code_lines: Optional[float]
    comment_lines: Optional[float]
    metrics: Optional[str]
    last_commit: Optional[str]
    last_commit_sha: Optional[str]
    has_wiki: Optional[bool]
    is_archived: Optional[bool]
    is_disabled: Optional[bool]
    is_locked: Optional[bool]
    languages: Optional[str]
    labels: Optional[str]
    topics: Optional[str]
    programming_language: Optional[str]
    file_commit_gram_scenarios: Optional[str]
    merge_scenarios: Optional[str]
    cherry_pick_scenarios: Optional[str]
    error: Optional[str]


@yt_dataclass
@dataclass
class SampleDataRow:
    id: str
    name: Optional[str]
    commits: Optional[int]
    branches: Optional[int]
    releases: Optional[int]
    forks: Optional[int]
    default_branch: Optional[str]
    license: Optional[str]
    watchers: Optional[int]
    stargazers: Optional[int]
    contributors: Optional[int]
    created_at: Optional[str]
    blank_lines: Optional[float]
    code_lines: Optional[float]
    comment_lines: Optional[float]
    last_commit: Optional[str]
    topics: Optional[str]
    programming_language: Optional[str]
    scenario: Optional[str]
    scenario_type: Optional[str]
    project_size: Optional[str]
    project_activity: Optional[str]

@yt_dataclass
@dataclass
class SampleDataRowV2:
    id: str
    name: Optional[str]
    commits: Optional[int]
    branches: Optional[int]
    releases: Optional[int]
    forks: Optional[int]
    default_branch: Optional[str]
    license: Optional[str]
    watchers: Optional[int]
    stargazers: Optional[int]
    contributors: Optional[int]
    created_at: Optional[str]
    blank_lines: Optional[float]
    code_lines: Optional[float]
    comment_lines: Optional[float]
    last_commit: Optional[str]
    topics: Optional[str]
    programming_language: Optional[str]
    scenario: Optional[str]
    scenario_type: Optional[str]
    project_size: Optional[str]
    project_activity: Optional[str]
    difficulty: Optional[str]

    def __init__(self, row: SampleDataRow, difficulty: str):
        self.id = row.id
        self.name = row.name
        self.commits = row.commits
        self.branches = row.branches
        self.releases = row.releases
        self.forks = row.forks
        self.default_branch = row.default_branch
        self.license = row.license
        self.watchers = row.watchers
        self.stargazers = row.stargazers
        self.contributors = row.contributors
        self.created_at = row.created_at
        self.blank_lines = row.blank_lines
        self.code_lines = row.code_lines
        self.comment_lines = row.comment_lines
        self.last_commit = row.last_commit
        self.topics = row.topics
        self.programming_language = row.programming_language
        self.scenario = row.scenario
        self.scenario_type = row.scenario_type
        self.project_size = row.project_size
        self.project_activity = row.project_activity
        self.difficulty = difficulty

@yt_dataclass
@dataclass
class SampleDataRowV3:
    id: str
    name: Optional[str]
    commits: Optional[int]
    branches: Optional[int]
    releases: Optional[int]
    forks: Optional[int]
    default_branch: Optional[str]
    license: Optional[str]
    watchers: Optional[int]
    stargazers: Optional[int]
    contributors: Optional[int]
    created_at: Optional[str]
    blank_lines: Optional[float]
    code_lines: Optional[float]
    comment_lines: Optional[float]
    last_commit: Optional[str]
    topics: Optional[str]
    programming_language: Optional[str]
    scenario: Optional[str]
    sample_type: Optional[str]
    project_size: Optional[str]
    project_activity: Optional[str]
    difficulty: Optional[str]

    def __init__(self, row: SampleDataRowV2):
        self.id = row.id
        self.name = row.name
        self.commits = row.commits
        self.branches = row.branches
        self.releases = row.releases
        self.forks = row.forks
        self.default_branch = row.default_branch
        self.license = row.license
        self.watchers = row.watchers
        self.stargazers = row.stargazers
        self.contributors = row.contributors
        self.created_at = row.created_at
        self.blank_lines = row.blank_lines
        self.code_lines = row.code_lines
        self.comment_lines = row.comment_lines
        self.last_commit = row.last_commit
        self.topics = row.topics
        self.programming_language = row.programming_language
        self.scenario = row.scenario
        self.sample_type = row.scenario_type
        self.project_size = row.project_size
        self.project_activity = row.project_activity
        self.difficulty = row.difficulty

@yt_dataclass
@dataclass
class SampleDataRowV4:
    id: str
    name: Optional[str]
    default_branch: Optional[str]
    license: Optional[str]
    stargazers: Optional[int]
    created_at: Optional[str]
    topics: Optional[str]
    programming_language: Optional[str]
    scenario: Optional[str]
    sample_type: Optional[str]
    project_size: Optional[str]
    project_activity: Optional[str]
    difficulty: Optional[str]

    def __init__(self, id: str, name: str, default_branch: str, license: str, stargazers: int,
                 created_at: str, topics: str, programming_language: str, scenario: str,
                 sample_type: str, project_size: str, project_activity: Optional[str],
                 difficulty: str):
        self.id = id
        self.name = name
        self.default_branch = default_branch
        self.license = license
        self.stargazers = stargazers
        self.created_at = created_at
        self.topics = topics
        self.programming_language = programming_language
        self.scenario = scenario
        self.sample_type = sample_type
        self.project_size = project_size
        self.project_activity = project_activity
        self.difficulty = difficulty

    # def __init__(self, row: SampleDataRowV3):
    #     self.id = row.id
    #     self.name = row.name
    #     self.default_branch = row.default_branch
    #     self.license = row.license
    #     self.stargazers = row.stargazers
    #     self.created_at = row.created_at
    #     self.topics = row.topics
    #     self.programming_language = row.programming_language
    #     self.scenario = row.scenario
    #     self.sample_type = row.sample_type
    #     self.project_size = row.project_size
    #     self.project_activity = row.project_activity
    #     self.difficulty = row.difficulty