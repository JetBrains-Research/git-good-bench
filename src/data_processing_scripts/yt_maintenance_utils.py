import os
import argparse
import yt.wrapper as yt
from dataclasses import asdict
from yt.wrapper.schema import TableSchema
from src.data_processing_scripts.mappers import ErrorFilteringMapper, MergeConflictMapper, \
    SelectOnlyMergeScenariosWithConflictsMapper, RemoveFileCommitGramScenariosWithMergesMapper, \
    SelectMergeScenariosWithExactlyTwoParents, ImproveMergeConflictScenarioQualityMapper, \
    DetermineFileCommitGramPurityMapper, TransformDatasetToOneRowPerSample, RemoveArchivedReposMapper, \
    RefineDatasetCoarse, RemoveFileCommitGramScenariosWithAddedFile, ClarifyDatasetMapper, RemoveUnneededMetadataMapper, \
    CheckIfFileCommitChainsContainNonPLFiles
from src.data_processing_scripts.mappers import RepositoryDataMapper
from src.data_processing_scripts.schemas import RepositoryDataRow, SampleDataRow, SampleDataRowV2, SampleDataRowV3, SampleDataRowV4
import pandas as pd

def parse_table_into_dataframe(table_path: str) -> pd.DataFrame:
    dataset = yt.read_table_structured(table=table_path, row_type=SampleDataRowV4)
    return pd.DataFrame([asdict(row) for row in dataset])

def parse_table_into_csv_at(output_path: str, table_path: str):
    dataset_df = parse_table_into_dataframe(table_path)
    dataset_df.to_csv(output_path)

def remove_duplicates_in(table_path: str, yt_client: yt.YtClient):
    dataset_df = parse_table_into_dataframe(table_path)
    dataset_df.drop_duplicates(inplace=True, subset=['name'])

    yt_client.remove(table_path)
    src_table_path = yt.TablePath(
        table_path,
        schema=TableSchema.from_row_type(RepositoryDataRow)
    )
    yt_client.write_table(
        table=src_table_path,
        input_stream=dataset_df.to_dict(orient="records"),
    )

def handle_errors_in_dataset(yt_client: yt.YtClient, src_table: str, dst_table: str):
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(RepositoryDataRow))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        ErrorFilteringMapper(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=10,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "cpu_limit": 1
            },
        },
    )


def detect_merge_conflicts_in(yt_client: yt.YtClient, src_table: str):
    dst_table = src_table + '_conflicts_detected'
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(RepositoryDataRow))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        MergeConflictMapper(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=700,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "memory_limit": 1 * 1024 ** 3,
                "memory_reserve_factor": 0.125,
                "tmpfs_size": 8 * 1024 ** 3,
                "tmpfs_path": "repos",
                "cpu_limit": 1
            }
        }
    )

def select_merge_scenarios_with_conflicts(yt_client: yt.YtClient, src_table: str):
    dst_table = '_'.join(src_table.split('_')[:-2]) + '_conflicts_selected'
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(RepositoryDataRow))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        SelectOnlyMergeScenariosWithConflictsMapper(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=10,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "cpu_limit": 1
            }
        }
    )

def select_merge_scenarios_with_exactly_two_parents(yt_client: yt.YtClient, src_table: str):
    dst_table = '/'.join(src_table.split('/')[:-1]) + '/cleaned_scraper_output_two_merge_parents'
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(RepositoryDataRow))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        SelectMergeScenariosWithExactlyTwoParents(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=50,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "cpu_limit": 1
            }
        }
    )

def remove_file_commit_gram_scenarios_with_merges(yt_client: yt.YtClient, src_table: str):
    dst_table = '_'.join(src_table.split('_')[:-2]) + '_chains_merges_selected'
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(RepositoryDataRow))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        RemoveFileCommitGramScenariosWithMergesMapper(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=3000,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "memory_limit": 1 * 1024 ** 3,
                "memory_reserve_factor": 0.125,
                "tmpfs_size": 8 * 1024 ** 3,
                "tmpfs_path": "repos",
                "cpu_limit": 1
            }
        }
    )

def run_repository_data_mapper(yt_client: yt.YtClient, src_table: str, dst_table: str):
    job_count = len(list(yt.read_table_structured(src_table, RepositoryDataRow)))

    yt_client.run_map(
        RepositoryDataMapper(sliding_window_size=3),
        src_table,
        dst_table,
        job_count=job_count,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "memory_limit": 4 * 1024 ** 3,
                "memory_reserve_factor": 0.125,
                "tmpfs_size": 1500 * 1024 ** 2,
                "tmpfs_path": "repos",
                "cpu_limit": 1
            },
        },
    )

def improve_merge_scenarios_with_conflicts(yt_client: yt.YtClient, src_table: str):
    dst_table = '_'.join(src_table.split('_')[:-3]) + '_pure_merge_conflicts'
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(RepositoryDataRow))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        ImproveMergeConflictScenarioQualityMapper(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=3000,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "memory_limit": 1 * 1024 ** 3,
                "memory_reserve_factor": 0.125,
                "tmpfs_size": 8 * 1024 ** 3,
                "tmpfs_path": "repos",
                "cpu_limit": 1
            }
        }
    )

def improve_file_commit_gram_quality(yt_client: yt.YtClient, src_table: str):
    dst_table = '_'.join(src_table.split('_')[:-3]) + '_purity_non_pl_file_commit_grams'
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(RepositoryDataRow))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        DetermineFileCommitGramPurityMapper(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=7932,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "memory_limit": 1 * 1024 ** 3,
                "memory_reserve_factor": 0.125,
                "tmpfs_size": 8 * 1024 ** 3,
                "tmpfs_path": "repos",
                "cpu_limit": 1
            }
        }
    )

def create_row_wise_dataset(yt_client: yt.YtClient, src_table: str):
    dst_table = '/'.join(src_table.split('/')[:-1] + ['dataset_row_wise_samples'])
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(SampleDataRow))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        TransformDatasetToOneRowPerSample(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=25,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "cpu_limit": 1
            }
        }
    )

def remove_archived_repos(yt_client: yt.YtClient, src_table: str):
    dst_table = '/'.join(src_table.split('/')[:-1] + ['pure_non_archived'])
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(RepositoryDataRow))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        RemoveArchivedReposMapper(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=5,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "cpu_limit": 1
            }
        }
    )

def refine_dataset_coarse(yt_client: yt.YtClient, src_table: str):
    dst_table = '/'.join(src_table.split('/')[:-1] + ['dataset_row_wise_samples_coarsely_refined'])
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(SampleDataRow))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        RefineDatasetCoarse(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=5,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "cpu_limit": 1
            }
        }
    )

def upload_dataset_to(table_path: str, path_to_local_dataset: str, yt_client: yt.YtClient):
    dataset_df = pd.read_csv(path_to_local_dataset, index_col=0)

    for c in dataset_df.columns:
        if len(dataset_df[dataset_df[c].isna()]) > 0 and dataset_df[c].dtype == 'object':
            dataset_df[c].fillna('not available', inplace=True)


    dst_table_path = yt.TablePath(
        table_path,
        schema=TableSchema.from_row_type(SampleDataRowV4)
    )
    yt_client.write_table(
        table=dst_table_path,
        input_stream=dataset_df.to_dict(orient="records"),
    )

def remove_file_commit_gram_scenarios_concerning_added_file(yt_client: yt.YtClient, src_table: str):
    dst_table = '/'.join(src_table.split('/')[:-1] + ['dataset_row_wise_samples_added_file_removed_difficulty_added'])
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(SampleDataRowV2))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        RemoveFileCommitGramScenariosWithAddedFile(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=1000,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "memory_limit": 1 * 1024 ** 3,
                "memory_reserve_factor": 0.125,
                "tmpfs_size": 8 * 1024 ** 3,
                "tmpfs_path": "repos",
                "cpu_limit": 1
            }
        }
    )

def clarify_dataset_mapper(yt_client: yt.YtClient, src_table: str):
    dst_table = src_table + '_upd'
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(SampleDataRowV3))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        ClarifyDatasetMapper(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=1,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "cpu_limit": 1
            }
        }
    )

def remove_unneeded_metadata_mapper(yt_client: yt.YtClient, src_table: str):
    dst_table = src_table + '_upd'
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(SampleDataRowV4))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        RemoveUnneededMetadataMapper(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=1,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "cpu_limit": 1
            }
        }
    )

def check_if_file_commit_chain_contains_non_pl_files_mapper(yt_client: yt.YtClient, src_table: str):
    dst_table = src_table + '_checked'
    dst_table_path = yt.TablePath(dst_table, schema=TableSchema.from_row_type(SampleDataRowV4))
    yt_client.create('table', dst_table_path)

    yt_client.run_map(
        CheckIfFileCommitChainsContainNonPLFiles(),
        source_table=src_table,
        destination_table=dst_table,
        job_count=1750,
        spec={
            "mapper": {
                "docker_image": "<docker image with python and ytsaurus and yson bindings>",
                "memory_limit": 1 * 1024 ** 3,
                "memory_reserve_factor": 0.125,
                "tmpfs_size": 8 * 1024 ** 3,
                "tmpfs_path": "repos",
                "cpu_limit": 1
            }
        }
    )

def main():
    parser = argparse.ArgumentParser(description='Process some tables in YTsaurus.')
    parser.add_argument('--src-table', type=str, help='Source table path')
    parser.add_argument('--dst-table', type=str, help='Destination table path')
    parser.add_argument('--csv-dataset-path', type=str, help='Path at which to persist CSV of dataset')
    args = parser.parse_args()

    yt_client = yt.YtClient(proxy=os.environ["YT_PROXY"], token=os.environ["YT_TOKEN"],
                            config={'pickling': {'ignore_system_modules': True}})
    parse_table_into_csv_at('../../data/git_good_bench.csv', args.src_table)

if __name__ == '__main__':
    main()
