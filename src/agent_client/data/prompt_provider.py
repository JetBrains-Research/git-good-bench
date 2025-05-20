from textwrap import dedent
from typing import Optional, Dict

from src.agent_client.environment.scenario_type import ScenarioType
from src.agent_client.utils.available_context import AvailableContext


class PromptProvider:
    _USER_PROMPT_CHUNK = """You are a staff software engineer with expertise in {programming_language} and Git. You are helping a junior team member who has been working all day without creating a commit to iteratively create commits and introduce their changes into the repository in a maintainable way. Help them to select hunks such that you can create multiple, small, but logically cohesive commits that are structurally sound, and follow best practices for maintainable code.

    Instructions:
    - Review the remaining hunks of code and help the junior engineer select the appropriate hunks for each commit.
    - Ensure that you select as many hunks as you need to ensure structural integrity, ie avoid breaking changes by, for example, removing a variable definition or initialization in one commit, but removing the usages of the variable in another commit.
    - Identify the ids of the hunks that you should pass by the number following "HUNK-" in the list of remaining hunks below. For HUNK-8, the id you need to pass, if you want to select this hunk, would be 8.
    - Each commit should be focused, small, and logically cohesive.
    - Provide a clear and concise commit message for each commit following the format provided in the example usages.

    Key Requirements:
    - Avoid apply all changes in a single commit unless you are absolutely sure this will yield the best possible git history.
    - You must always fill all parameters of the provided tools. This includes the "reason" parameter.

    Process all of the following {number_of_remaining_hunks} hunks:
    {remaining_hunks}

    Task:
    Pass a list of hunks to include in the commit and a descriptive commit message to the provided tool.
    
    You must only use the following tools and follow their specification exactly and always provide a reason for calling a tool.
    All tools other than the ones listed below are INVALID and you MUST NOT use them under any circumstances.
    Valid tools:
    - commit_changes_in
    - commit_remaining_changes
    
    Example usages:
    commit_changes_in(selected_hunks=[1,3], commit_message="FIX: Handle edge case of uninitialized object",
                         reason="to group the fixing of uninitialized objects together")
    commit_changes_in(selected_hunks=[4], commit_message="ADD: Introduced new enum class CarConfiguration",
                         reason="to isolate the addition of the new enum class")
    commit_changes_in(selected_hunks=[2,5], commit_message="REFACTOR: Migrate car configurator to CarConfiguration enum",
                         reason="The remaining changes both deal with migrating the existing implementation to the enum introduced in the previous commits. This way the commits build on each other in a logical progression and the migration takes place once we ensure that the class we migrate to is already present, thus avoiding breaking changes.")
    
    Once you have received a signal that you are done, you must always call the tool in the example below to terminate:
    commit_remaining_changes(commit_message="UPDATE: Implement data streaming feature",
            reason="because all hunks were processed and I must now terminate")                     
    """

    _TODO_LIST_JSON_SCHEMA = """{{
        "type": "json",
        "schemaName": "rebase-todo-list-item",
        "schema": {{
            "type": "object",
            "properties": {{
                    "commit_index": {{"type": "integer"}},
                    "command": {{"enum": ["pick", "drop", "fixup", "fixup -c", "squash", "reword"]}},
                    "commit_msg": {{"type": "string"}},
                }}
            }},
            "required": ["operations"],
            "additionalProperties": False
    }}"""

    _USER_PROMPT_REBASE = """
    You are a staff software engineer with expertise in {programming_language} and Git.
    You are helping a junior team member who has been committing all day without pushing their commits to the remote. 
    Help them perform an interactive rebase to clean up their local tree. The rebase has already been initiated for you 
    and is currently paused so that you can inspect the commits participating in the rebase and edit the rebase todo list.

    The commits involved in the rebase are listed below. When referring to them in function calls, use the commit index "i" to refer 
    to <COMMIT-i>. Avoid viewing all commits again, they are already presented below. The commits are delimited by the <COMMIT-i> and </COMMIT-i> tags:
    {participating_commits}

    Instructions:
    Consider the changes in the commits and make adjustments if necessary such that the local tree:
    - contains logically cohesive commits
    - all commits have meaningful, descriptive commit messages that follow a unified format
    - does not contain commits with duplicate commit messages
    - follows best practices for maintainable code

    You must only use the following tools and follow their specification exactly. Always provide a reason for calling a tool.
    List of valid tools for this scenario: 
    - view_rebase_todo: View current rebase todo list
    - execute_rebase: Execute the rebase with the current rebase todo list, thereby all rebase-todo-list-items are processed in an ascending order
    - show_changes_in: If you want to spend more time thinking about some of the presented commits, use this tool to inspect the changes introduced by commit with index i
        Below are some examples of how to use this function:
        show_changes_in(commit_index=4, reason='to inspect the changes in COMMIT-4')
        show_changes_in(commit_index=0, reason='to understand how the changes in COMMIT-0 relate to its commit message')
    - update_rebase_todo_list: Update the rebase todo list, reordering items or adjusting the commands to perform on commits.
        Each item in the list that you must pass to update_rebase_todo_list must be a string that complies with the
        rebase-todo-list-item JSON schema specified below:
        {rebase_todo_list_item_schema}

        Below are some examples of how to use this function:
        Note: Positioning the rebase todo item with index 2 at the first position in the list, 
        will swap it to the topmost position in the rebase todo list
        update_rebase_todo_list(rebase_todo_list_items=[
            '{{"commit_index": 2, "command": "pick"}}',
            '{{"commit_index": 1, "command": "reword", "commit_msg": "FIX: Explicitly handle division by zero edge case"}}',
            '{{"commit_index": 0, "command": "fixup"}}',
            '{{"commit_index": 3, "command": "pick"}}',
            '{{"commit_index": 4, "command": "drop"}}'
        ], reason='to remove an unnecessary, noise, experimental commit, improve the commit message of COMMIT-1 and consolidate 
        the changes in COMMIT-0 and COMMIT-1')

        Note: Example for a different sample, you must ensure to always have exactly one item per commit.
        update_rebase_todo_list(rebase_todo_list_items=[
            '{{"commit_index": 0, "command": "pick"}}',
            '{{"commit_index": 2, "command": "squash", "commit_msg": "ADD: Define interfaces and test cases for ShoppingBasketService"}}',
            '{{"commit_index": 1, "command": "pick"}}'
        ], reason='to reorder the local tree, yielding more coherent and logical increments of changes in the local tree and to consolidate the changes in COMMIT-0 and COMMIT-2')

    Only the following commands are allowed for the rebase todo list items. Make sure to only provide the required fields for each command, 
    all fields other than the required fields are invalid:
    - pick: Use this commit as is. Required fields: ["commit_index", "command"]
    - drop: Remove this commit. Required fields: ["commit_index", "command"]
    - fixup: Meld this commit into previous commit, reducing the total amount of commits by 1. Only keep the previous commit's log message. Required fields: ["commit_index", "command"]
    - fixup -C: Meld this commit into previous commit, reducing the total amount of commits by 1. Only keep this commit's log message. Required fields: ["commit_index", "command"]
    - squash: Meld this commit into previous commit, reducing the total amount of commits by 1. Commit message of resulting commit must be specified. Required fields: ["commit_index", "command", "commit_msg"]
    - reword: Use commit, but edit commit message. Commit message must be specified. Required fields: ["commit_index", "command", "commit_msg"]

    Key Requirements:
    - You must not simply pick all commits without modifying anything in the rebase todo list. Do your best to improve the local tree however you see fit.
    - Avoid squashing all commits into a single commit, consider for which commits this would improve the resulting commit history.
    - Try to consolidate the total size of the local tree such that the resulting tree has length k<{times_seen_consecutively}
    - You must always fill all parameters of the provided tools. This includes the "reason" parameter.
    """

    _MERGE_PROMPT_TASK_OVERVIEW = """You are a staff software engineer with expertise in {programming_language} and git. 
    You are helping a junior team member who has initiated a merge that resulted in one or more 
    merge conflicts in one or more files. Your task is to help you junior colleague with resolving 
    all {total_amount_of_merge_conflicts} merge conflicts.

    The semantic meaning and temporal relationship of the two sides of the merge conflicts are as 
    follows for ALL merge conflicts you will encounter:
    {commit_temporal_ordering}
    """

    _MERGE_PROMPT_INSTRUCTION_DETAILS="""- Consider the context of the temporal relationship of the branches that are being merged and the intent of the junior developer, 
        with respect to which side of the conflict contains the local and which the incoming changes. The intent of the developer 
        is to merge the incoming changes into the local changes.
    """

    _MERGE_PROMPT_SCENARIO_SPECIFIC_RESOLUTION_EXEMPLARS="""resolve_current_merge_conflict_with(content='from app.api.auth import PremiumUser\\n', reason='The premium user class 
        is a new authentication class that is being used in the incoming changes and thus is most likely part of what 
        the junior developer wants to have access to')
    resolve_current_merge_conflict_with(content='    bool debug = conf.shouldDebug;\\n    bool enableCaching = conf.enableCaching;\\n    bool shouldRetry = conf.shouldRetry;\\n', 
        reason='both of these configuration flags are being used in the local changes, also I fixed a copy-paste bug and
         now the enableCaching flag is correctly initialized to conf.enableCaching. The shouldRetry flag 
         is an incoming change that conflicts with what the developer introduced, I will thus keep all three flags.')
    """

    _USER_PROMPT_MERGE_CONFLICT = """{task_overview}

    The following files have merge conflicts:
    {files_with_conflicts}

    Below are all merge conflicts that need to be resolved, delimited by <CONFLICT-i> tags where i is the 0-based index:
    {all_merge_conflicts}

    Instructions:
    - Start with resolving the conflict at index 0 (CONFLICT-0) and proceed in ascending order through the conflicts.
        CONFLICT-0 is the current conflict that needs to be resolved.
    - Consider the context around the merge conflicts, of the overall diffs and files in which the conflicts occur.
    - Resolve the conflicts in a cohesive manner. For example, if you remove a function in a conflict, make sure that
        you also remove any invocations of that function in any other conflicts.
    - If you are just choosing one of the two sides, without changing any of the actual content, make sure to also reproduce
        the whitespaces exactly.
    - If the merge conflict occurs due to a NOP (e.g. one side of the conflict is empty, the other is a commented code block)
        favor resolving the conflict to the most maintainable and concise way. Avoid dead code.
    - Make sure to consider the implications your previous resolutions have on the remaining resolutions, especially when 
        resolving multiple conflicts in a single file.
    - If you find simple bugs, such as typos, copy and paste errors in variable assignments or parameters, feel free to 
        help your junior developer fix these. Do not perform complex refactorings or attempt to change code drastically. 
        Make as few changes to the side that you are accepting as possible.
    {instruction_details}

    You must only use the following tools and follow their specification exactly and always provide a reason for calling a tool.
    All tools other than the ones listed below are INVALID and you MUST NOT use them under any circumstances.
    Valid tools: 
    - view_current_merge_conflict_with
    - view_merge_conflict_at
    - resolve_current_merge_conflict_with
    - view_diff_for
    - view_file_at: You must not use this command more than once per file as it is costly.

    Below follow some examples detailing the usage of the above tools:
    view_current_merge_conflict_with(context_window_size=15, reason='to get a more comprehensive overview of the local context around the current merge conflict')
    view_current_merge_conflict_with(context_window_size=0, reason='to view only the current merge conflict without any local context')
    view_current_merge_conflict_with(context_window_size=5, reason='to view only the current merge conflict with some local context')
    view_merge_conflict_at(conflict_index=1, context_window_size=5, 
        reason='To ensure that the resolution for CONFLICT-0 is cohesive with CONFLICT-1')
    view_merge_conflict_at(conflict_index=1, context_window_size=10, 
        reason='To remind myself of the changes and context around CONFLICT-3 so that I can decide whether to delete 
            the import for ShoppingClient in the current conflict')
    view_diff_for(relative_path_from_project_root='src/app/io/FileParser.java', reason='view the full diff between 
        the local and incoming changes for the file at path')
    view_diff_for(relative_path_from_project_root='src/app/api/quantative_methods/regression.python', 
        reason='understand how to resolve the current conflict such that the resolution is cohesive and makes sense 
        in the context of the overall changes')
    view_file_at(relative_path_from_project_root='src/tests/test_data_transformations.py', reason='understand the full 
        context of the merge conflict, because I think I might have found a small bug, but I need more context to make 
        sure it is one before applying a minor fix as part of the conflict resolution.')
    view_file_at(relative_path_from_project_root='src/app/utils.py', reason='to check whether there are other commented 
        out code blocks')
    view_file_at(relative_path_from_project_root='src/app/Authenticator.java', reason='to see how the changes I made so far fit 
        into the file and to ensure I resolve the current conflicts such that it is cohesive with these previous 
        resolutions')
    {scenario_type_specific_resolution_exemplars}

    Key Requirements:
    - Once the conflicts are resolved you are done and must terminate.
    - Your decisions must be cohesive across merge conflicts.
    - Make sure that all your lines end with a newline character to avoid introducing accidental changes.
    - You must always fill all parameters of the provided tools. This includes the "reason" parameter."""

    _SYSTEM_PROMPT = dedent("""
                You MUST follow the instructions for answering:
                - You are an agent which can operate with the command line and change the file system.
                - You need to execute the given task with the best quality.
                - I have no fingers and the placeholders trauma. Return the entire code template for an answer when needed. NEVER use placeholders.
                - You ALWAYS will be PENALIZED for wrong and low-effort answers.
                - I'm going to tip $1,000,000 for the best reply.
                - Your answer is critical for my career.
                - YOU MUST USE THE PROVIDED TOOLS TO ACTUALLY CHANGE THE FILE SYSTEM.
                """)

    @classmethod
    def get_system_prompt(cls):
        return cls._SYSTEM_PROMPT

    @classmethod
    def get_prompt_for(cls, scenario_type: ScenarioType, scenario: Dict, context: Optional[Dict]):
        """
        Constructs a prompt for the given task based on the provided scenario type and specifications.

        Args:
            scenario_type (ScenarioType): The type of scenario to get the prompt for.
            scenario (Dict): The actual specifications of the scenario for the given scenario type.
            context (Optional[Dict]): Additional context for the model to orient itself in the repository. E.g.: git status,
                git diff, truncated git log etc. The only allowed keys of this dict are members of AvailableContext.

        Raises:
            ValueError if called by an invalid scenario type.

        Returns:
            str: Returns appropriate response for the given scenario type.
        """
        if scenario_type is ScenarioType.FILE_COMMIT_CHAIN_CHUNK:
            return cls._USER_PROMPT_CHUNK.format(number_of_remaining_hunks=context[AvailableContext.REMAINING_HUNKS][0],
                                                 remaining_hunks=context[AvailableContext.REMAINING_HUNKS][1],
                                                 programming_language=context[AvailableContext.PROGRAMMING_LANGUAGE])
        elif scenario_type is ScenarioType.FILE_COMMIT_CHAIN_REBASE:
            return cls._USER_PROMPT_REBASE.format(participating_commits=context[AvailableContext.REBASE_PARTICIPATING_COMMITS],
                                                  times_seen_consecutively=scenario['times_seen_consecutively'],
                                                  programming_language=context[AvailableContext.PROGRAMMING_LANGUAGE],
                                                  rebase_todo_list_item_schema=cls._TODO_LIST_JSON_SCHEMA)
        elif scenario_type is ScenarioType.MERGE:
            return cls._USER_PROMPT_MERGE_CONFLICT.format(files_with_conflicts=context[AvailableContext.FILES_WITH_CONFLICTS],
                                                 all_merge_conflicts=context[AvailableContext.ALL_MERGE_CONFLICTS],
                                                 task_overview=cls._MERGE_PROMPT_TASK_OVERVIEW.format(
                                                     commit_temporal_ordering=context[AvailableContext.COMMIT_TEMPORAL_ORDERING],
                                                     programming_language=context[
                                                         AvailableContext.PROGRAMMING_LANGUAGE],
                                                     total_amount_of_merge_conflicts=context[
                                                         AvailableContext.TOTAL_AMOUNT_OF_MERGE_CONFLICTS]
                                                 ),
                                                 instruction_details=cls._MERGE_PROMPT_INSTRUCTION_DETAILS,
                                                 scenario_type_specific_resolution_exemplars=cls._MERGE_PROMPT_SCENARIO_SPECIFIC_RESOLUTION_EXEMPLARS)
        else:
            return ValueError('No other scenarios are valid.')
