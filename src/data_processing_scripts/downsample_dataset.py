import ast
import re

import pandas as pd

def bin_file_commit_chain_purity(scenario):
    if scenario['purity'] == 1:
        return 'easy'
    elif 1 > scenario['purity'] >= 0.75:
        return 'medium'
    elif 0.75 > scenario['purity'] >= 0.5:
        return 'hard'

def bin_merge_conflict_difficulty(scenario):
    if scenario['number_of_files_with_merge_conflict'] == 1 and scenario['total_number_of_merge_conflicts'] == 1:
        return 'easy'
    elif scenario['number_of_files_with_merge_conflict'] == 1 and scenario['total_number_of_merge_conflicts'] > 1:
        return 'medium'
    elif scenario['number_of_files_with_merge_conflict'] > 1 and scenario['total_number_of_merge_conflicts'] > 1:
        return 'hard'

def sample_by_strata(df, sample_size=100):
    sampled_groups = []
    grouping_cols = ['programming_language']

    for group_keys, group in df.groupby(grouping_cols):
        print(f"Processing group: {group_keys}")
        if len(group) < sample_size:
            print(f"Group {group_keys} has less than 100 samples ({len(group)}), using all available.")
            sampled_groups.append(group)
        else:
            # Within each group, perform stratified sampling based on the strata column.
            strata_groups = list(group.groupby("strata"))

            # Allocate samples proportionally to each stratum.
            total_count = len(group)
            samples_per_stratum = {}
            for strata_val, subgroup in strata_groups:
                # Compute allocation proportional to the subgroup count (ensuring at least one sample per stratum)
                n_samples = max(1, round((len(subgroup) / total_count) * 100))
                samples_per_stratum[strata_val] = min(n_samples, len(subgroup))

            subgroup_samples = []
            for strata_val, subgroup in strata_groups:
                n = samples_per_stratum.get(strata_val, 0)
                if n > 0:
                    sampled_subgroup = subgroup.sample(n=n, random_state=42)
                    subgroup_samples.append(sampled_subgroup)

            group_sample = pd.concat(subgroup_samples)
            # Adjust the total number of samples to exactly 100 if necessary.
            if len(group_sample) > sample_size:
                group_sample = group_sample.sample(n=sample_size, random_state=42)
            elif len(group_sample) < sample_size:
                extra_needed = sample_size - len(group_sample)
                remaining = group.drop(group_sample.index)
                if len(remaining) >= extra_needed:
                    extra = remaining.sample(n=extra_needed, random_state=42)
                    group_sample = pd.concat([group_sample, extra])
                else:
                    group_sample = pd.concat([group_sample, remaining])
            sampled_groups.append(group_sample)

    return sampled_groups

def main():
    path_to_dataset = '../../data/git_good_bench_full.csv'
    input_dataset = pd.read_csv(path_to_dataset, index_col=0)

    input_dataset['scenario'] = input_dataset['scenario'].apply(lambda row: ast.literal_eval(row))

    input_dataset["repository_slug"] = input_dataset["name"].apply(lambda row: re.sub(r"[^\w]+", "_", row).strip("_"))
    input_dataset["sample_index"] = input_dataset.groupby(["name", "sample_type"]).cumcount()
    input_dataset["id"] = input_dataset.apply(
        lambda row: f"{row.repository_slug}_{row.sample_type}_{row.sample_index:04d}",
        axis=1
    )
    input_dataset.drop(columns=['sample_index', 'repository_slug'], inplace=True)

    file_commit_chain_df = input_dataset[input_dataset['sample_type'] == 'file_commit_chain']
    merge_df = input_dataset[input_dataset['sample_type'] == 'merge']

    file_commit_chain_df['file_commit_chain_purity'] = file_commit_chain_df['scenario'].apply(
        lambda row: bin_file_commit_chain_purity(row))
    merge_df['merge_conflict_difficulty'] = merge_df['scenario'].apply(
        lambda row: bin_merge_conflict_difficulty(row))

    stratify_cols = ['name', 'project_size']
    file_commit_chain_df["strata"] = file_commit_chain_df.apply(lambda row: "_".join(
        [str(row[col]) for col in stratify_cols] + [str(row['file_commit_chain_purity'])]
    ), axis=1)

    merge_df["strata"] = merge_df.apply(lambda row: "_".join(
        [str(row[col]) for col in stratify_cols] + [str(row['merge_conflict_difficulty'])]
    ), axis=1)

    merge_sample = pd.concat(sample_by_strata(merge_df, 20))
    file_commit_chain_sample = pd.concat(sample_by_strata(file_commit_chain_df, 20))

    git_good_bench_lite = pd.concat([merge_sample, file_commit_chain_sample]).drop(columns=['strata', 'project_activity',
                                                                                      'file_commit_chain_purity',
                                                                                      'merge_conflict_difficulty'])

    git_good_bench_lite['topics'] = git_good_bench_lite['topics'].apply(
        lambda row: row if row not in ['None', 'none', 'nan', 'NaN'] and not pd.isna(row) else 'unavailable')

    # Drop used samples
    merge_df.drop(merge_sample.index, inplace=True)
    file_commit_chain_df.drop(file_commit_chain_sample.index, inplace=True)

    # Compute second dataset version
    merge_sample = pd.concat(sample_by_strata(merge_df, 150))
    file_commit_chain_sample = pd.concat(sample_by_strata(file_commit_chain_df, 150))

    git_good_bench = pd.concat([merge_sample, file_commit_chain_sample]).drop(columns=['strata',
                                                                                      'file_commit_chain_purity',
                                                                                      'merge_conflict_difficulty'])

    git_good_bench['topics'] = git_good_bench['topics'].apply(
        lambda row: row if row not in ['None', 'none', 'nan', 'NaN'] and not pd.isna(row) else 'unavailable')

    # Ensure git_good_bench and git_good_bench_lite are disjoint
    assert not set(git_good_bench['id']) & set(git_good_bench_lite['id'])

    # Remove test set rows from train split
    excluded_ids = set(git_good_bench_lite['id']).union(set(git_good_bench['id']))
    input_dataset = input_dataset[~input_dataset['id'].isin(excluded_ids)]
    input_dataset.to_csv('../../data/git_good_bench_train.csv')

    # Persist datasets
    git_good_bench.to_csv('../../data/git_good_bench.csv')
    git_good_bench_lite.to_csv('../../data/git_good_bench_lite.csv')

if __name__ == "__main__":
    main()