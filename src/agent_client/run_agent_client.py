import ast
import asyncio
import logging
import os
import sqlite3
import time

from src.agent_client.data.git_dataset_provider import GitDatasetProvider
from src.agent_client.data.prompt_provider import PromptProvider
from src.agent_client.data.yt_connection_manager import YTConnectionManager
from src.agent_client.environment.docker_manager import DockerManager
from src.agent_client.environment.evaluator import Evaluator
from src.agent_client.environment.scenario_environment_manager import ScenarioEnvironmentManager
from src.agent_client.environment.scenario_type import ScenarioType
from src.agent_client.environment.terminal_access_tool_provider import TerminalAccessToolImplementationProvider
from src.agent_client.utils.available_context import AvailableContext
from src.agent_client.utils.exceptions import ScenarioEnvironmentException

async def main():
    yt_connection_manager = YTConnectionManager(dataset_table_location=os.environ['YT_DATASET_TABLE_LOCATION'])
    response = yt_connection_manager.get_dataset_stream()
    git_dataset_provider = GitDatasetProvider(response)

    try:
        connection = sqlite3.connect('data/vcs-agent-evaluation-metadata.db')
        cursor = connection.cursor()
        cursor.execute('PRAGMA synchronous = FULL;')
        cursor.execute('CREATE TABLE IF NOT EXISTS vcs_agent_evaluation_metadata (sample_id TEXT, '
                       'agent_solution TEXT NOT NULL, ground_truth TEXT NOT NULL, is_solved INTEGER NOT NULL,'
                       ' llm_responses TEXT, scenario_type TEXT NOT NULL, execution_time_ms INTEGER NOT NULL,'
                       ' primary key (sample_id, scenario_type))')

        # Get list of processed sample_ids
        cursor.execute('SELECT sample_id, COUNT(scenario_type) as \'processed_scenario_types\' FROM vcs_agent_evaluation_metadata GROUP BY sample_id')
        processed_sample_ids = {row[0]: row[1] for row in cursor.fetchall()}
    except sqlite3.Error as e:
        logging.error(f"Failed to initialize database: {e}")
        raise

    requested_contexts_for = {
        ScenarioType.FILE_COMMIT_CHAIN_CHUNK: [AvailableContext.GIT_DIFF,
                                              AvailableContext.GIT_STATUS,
                                              AvailableContext.PROGRAMMING_LANGUAGE,
                                              AvailableContext.REMAINING_HUNKS],
        ScenarioType.FILE_COMMIT_CHAIN_REBASE: [AvailableContext.PROGRAMMING_LANGUAGE,
                                               AvailableContext.REBASE_PARTICIPATING_COMMITS],
        ScenarioType.MERGE: [AvailableContext.PROGRAMMING_LANGUAGE,
                             AvailableContext.COMMIT_TEMPORAL_ORDERING,
                             AvailableContext.TOTAL_AMOUNT_OF_MERGE_CONFLICTS,
                             AvailableContext.FILES_WITH_CONFLICTS,
                             AvailableContext.ALL_MERGE_CONFLICTS],
        ScenarioType.CHERRY_PICK: [AvailableContext.PROGRAMMING_LANGUAGE,
                                   AvailableContext.COMMIT_TYPE, # cherry or dst
                                   AvailableContext.TOTAL_AMOUNT_OF_MERGE_CONFLICTS,
                                   AvailableContext.FILES_WITH_CONFLICTS,
                                   AvailableContext.ALL_MERGE_CONFLICTS]
    }

    docker_manager = DockerManager(
        image='tolindenba/ytsaurus:python-3.10',
        env_vars={},
        container_start_timeout=300,
    )

    docker_manager.setup_image()
    container = docker_manager.run_container()

    # Disable editor to prevent interactive commands from opening an editor
    disable_editor_command = 'git config --global core.editor "true" && chmod u+x sequence_editor.sh'
    err_code, output = container.exec_run(f'/bin/bash -c "{disable_editor_command}"', privileged=False)
    if err_code != 0:
        raise ScenarioEnvironmentException('Could not disable editor and setup script for pausing interactive rebase. '
                                           f'Error code: {err_code}, Output: {output}')

    llm_client = ...

    for sample in git_dataset_provider.stream_samples():
        if sample.id in processed_sample_ids and 'merge' in sample.id:
            logging.info(f"Skipping scenario merge {sample.id} because it has already been processed.")
            continue
        elif sample.id in processed_sample_ids and processed_sample_ids[sample.id] == 2:
            logging.info(f"Skipping scenario file-commit chain {sample.id} because it has already been processed.")
            continue
        host_agent_work_dir = os.path.join(os.getcwd(), docker_manager.agent_repo_dir, sample.name.split('/')[-1])
        try:
            scenario_environment_manager = ScenarioEnvironmentManager(
                container=container,
                sample=sample,
                host_agent_work_dir=host_agent_work_dir
            )
            scenario_environment_manager.setup_repository()
        except ScenarioEnvironmentException as e:
            logging.error(f"Skipping scenario {sample.id}: \n{e}")
            continue
        except ValueError as e:
            logging.error(f"Skipping scenario {sample.id}. Could not set repository working directory: \n{e}")
            continue

        evaluator = Evaluator(container=container,
                              agent_target_branch_name=scenario_environment_manager.AGENT_TARGET_BRANCH_NAME,
                              repository_work_dir=scenario_environment_manager.repository_work_dir,
                              llm_client=llm_client,
                              host_agent_work_dir=host_agent_work_dir)

        scenario = ast.literal_eval(sample.scenario)

        if sample.sample_type == ScenarioType.CHERRY_PICK.value:
            scenario_types = [ScenarioType.CHERRY_PICK]
        elif sample.sample_type == ScenarioType.MERGE.value:
            scenario_types = [ScenarioType.MERGE]
        else:
            scenario_types = [ScenarioType.FILE_COMMIT_CHAIN_CHUNK, ScenarioType.FILE_COMMIT_CHAIN_REBASE]

        for scenario_type in scenario_types:
            start_time = time.time()
            try:
                scenario_environment_manager.set_scenario(scenario)
                scenario_environment_manager.set_scenario_type(scenario_type)
                scenario_environment_manager.setup_scenario_preconditions()
            except ScenarioEnvironmentException as e:
                logging.error(f"Skipping scenario {sample} due to precondition setup error: \n{e}")
                scenario_environment_manager.teardown_scenario()
                try:
                    cursor.execute('INSERT OR REPLACE INTO vcs_agent_evaluation_metadata VALUES (?,?,?,?,?,?,?)',
                                   (sample.id, str(e),
                                    str(e), int(False),
                                    None,
                                    scenario_type.value, -1))
                    connection.commit()
                except sqlite3.IntegrityError as e:
                    logging.error(f"Could not persist metadata for {sample.id} due to integrity error: \n{e}")
                except Exception as e:
                    logging.error(f"Could not persist metadata for {sample.id} due to unexpected error: \n{e}")
                    raise e
                continue

            system_prompt = PromptProvider.get_system_prompt()

            try:
                scenario_context = scenario_environment_manager.provide_scenario_context(requested_contexts_for[scenario_type])
                if AvailableContext.PROGRAMMING_LANGUAGE in requested_contexts_for[scenario_type]:
                    scenario_context[AvailableContext.PROGRAMMING_LANGUAGE] = sample.programming_language

                user_prompt = PromptProvider.get_prompt_for(scenario_type, scenario, context=scenario_context)
            except ScenarioEnvironmentException as e:
                logging.error(f"Could not fetch scenario context for repository {sample.name}, scenario type "
                              f"{scenario_type} and\nscenario{scenario}:\n{e}\n"
                              'Proceeding without context.')
                user_prompt = PromptProvider.get_prompt_for(scenario_type, scenario, context=None)

            logging.debug(f'Current scenario is given by:\nRepository: {sample.name}\nScenario type: {scenario_type}'
                          f'\nScenario: {scenario}\nUser prompt: {user_prompt}')

            tool = TerminalAccessToolImplementationProvider(
                container=container,
                error_message=None,
                max_num_chars_bash_output=30000,
                bash_timeout=180,
                workdir=scenario_environment_manager.repository_work_dir,
                scenario_environment_manager=scenario_environment_manager
            )

            # TODO Define your agent and run it here

            unexpected_exception = None
            try:
                await runner.arun()
            except Exception as e:
                unexpected_exception = f"Error running scenario {sample.id} for scenario type {scenario_type}: \n{e}"
                logging.error(unexpected_exception)

            evaluator.set_scenario(scenario)
            evaluator.set_scenario_type(scenario_type)

            scenario['repository'] = sample.name
            try:
                result = evaluator.evaluate()
                evaluation_metadata = evaluator.get_evaluation_metadata()
            except Exception as e:
                unexpected_exception = f"Error evaluating scenario for {sample.id} for scenario type {scenario_type}: \n{e}"
            finally:
                try:
                    end_time = time.time()
                    execution_time_ms = int((end_time - start_time) * 1000)

                    if not evaluation_metadata['agent_solution'] and not unexpected_exception:
                        unexpected_exception = 'Agent was terminated by hosting platform due to an unexpected error. Potentially because the agent failed to correctly fill the function parameters before a timeout.'

                    cursor.execute('INSERT OR REPLACE INTO vcs_agent_evaluation_metadata VALUES (?,?,?,?,?,?,?)',
                                   (sample.id, evaluation_metadata['agent_solution'] if not unexpected_exception else unexpected_exception,
                                    evaluation_metadata['ground_truth'] if not unexpected_exception else unexpected_exception, int(result), evaluation_metadata['llm_responses'],
                                    scenario_type.value, execution_time_ms))
                    connection.commit()
                except sqlite3.IntegrityError as e:
                    logging.error(f"Could not persist metadata for {sample.id} due to integrity error: \n{e}")
                except Exception as e:
                    logging.error(f"Could not persist metadata for {sample.id} due to unexpected error: \n{e}")

            if result:
                logging.info('Yay, successfully resolved this scenario!')
            else:
                logging.info('Could not resolve this scenario.')

            try:
                scenario_environment_manager.teardown_scenario()
            except ScenarioEnvironmentException as e:
                logging.error(f"Scenario cleanup failed for {scenario}: \n{e}\n"
                              f"Attempting to recover by removing and re-setting (incl. clone) the repository.")
                try:
                    scenario_environment_manager.teardown_repository()
                    scenario_environment_manager.setup_repository()
                except ScenarioEnvironmentException:
                    logging.error(f'Could not recover for scenario: {scenario}. Continuing with the next repository.')
                    break

        # If this raises an exception the only way out would be re-orchestrating the Docker container, or removing with force
        # Neither of which I really want to do for now.
        scenario_environment_manager.teardown_repository()

    try:
        cursor.close()
        connection.close()
    except Exception as e:
        logging.error(f"Error closing database connection: {e}")

if __name__ == '__main__':
    asyncio.run(main())
