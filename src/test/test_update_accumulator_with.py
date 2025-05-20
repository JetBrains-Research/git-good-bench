import unittest
import os
from git import Repo
from sys import path

path.append("..")
from src.repository_data_scraper.repository_data_scraper import RepositoryDataScraper
from src.repository_data_scraper.programming_language import ProgrammingLanguage


class UpdateAccumulatorWithTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.chdir('../..')
        cls.path_to_repositories = os.path.join(os.getcwd(), 'repos', 'testing-repositories')

    def setUp(self):
        demo_repo = Repo(os.path.join(UpdateAccumulatorWithTestCase.path_to_repositories, 'demo-repo.git'))
        os.chdir(os.path.join(UpdateAccumulatorWithTestCase.path_to_repositories, 'demo-repo.git'))

        self.repository_data_scraper = RepositoryDataScraper(repository=demo_repo,
                                                             programming_language=ProgrammingLanguage.TEXT,
                                                             repository_name='demo-repo',
                                                             sliding_window_size=4)


    def tearDown(self):
        self.repository_data_scraper = None

    def test_should_add_file_with_counter_greater_than_sliding_window_size_to_accumulator(self):
        branch = 'test_branch'
        file = 'demo_file.py'
        self.repository_data_scraper.state = {
            branch: {
                file: {
                    'file': file,
                    'branch': branch,
                    'oldest_commit': 'commit_hash_1',
                    'newest_commit': 'commit_hash_2',
                    'times_seen_consecutively': 5
                }
            }
        }

        self.assertTrue(len(self.repository_data_scraper.accumulator['file_commit_chain_scenarios']) == 0)

        self.repository_data_scraper.update_accumulator_with_file_commit_chain_scenario(
            self.repository_data_scraper.state[branch][file],
            file, branch)

        self.assertTrue(len(self.repository_data_scraper.accumulator['file_commit_chain_scenarios']) == 1)
        self.assertEqual(self.repository_data_scraper.accumulator['file_commit_chain_scenarios'][0],
                         self.repository_data_scraper.state[branch][file])

    def test_should_add_file_with_counter_equal_to_sliding_window_size_to_accumulator(self):
        branch = 'test_branch'
        file = 'demo_file.py'
        self.repository_data_scraper.state = {
            branch: {
                file: {
                    'file': file,
                    'branch': branch,
                    'oldest_commit': 'commit_hash_1',
                    'newest_commit': 'commit_hash_2',
                    'times_seen_consecutively': 4
                }
            }
        }

        self.assertTrue(len(self.repository_data_scraper.accumulator['file_commit_chain_scenarios']) == 0)

        self.repository_data_scraper.update_accumulator_with_file_commit_chain_scenario(
            self.repository_data_scraper.state[branch][file],
            file, branch)

        self.assertTrue(len(self.repository_data_scraper.accumulator['file_commit_chain_scenarios']) == 1)
        self.assertEqual(self.repository_data_scraper.accumulator['file_commit_chain_scenarios'][0],
                         self.repository_data_scraper.state[branch][file])

    def test_should_not_add_file_with_counter_less_than_sliding_window_size_to_accumulator(self):
        branch = 'test_branch'
        file = 'demo_file.py'
        self.repository_data_scraper.state = {
            branch: {
                file: {
                    'file': file,
                    'branch': branch,
                    'oldest_commit': 'commit_hash_1',
                    'newest_commit': 'commit_hash_2',
                    'times_seen_consecutively': 3
                }
            }
        }

        self.assertTrue(len(self.repository_data_scraper.accumulator['file_commit_chain_scenarios']) == 0)

        self.repository_data_scraper.update_accumulator_with_file_commit_chain_scenario(
            self.repository_data_scraper.state[branch][file],
            file, branch)

        self.assertTrue(len(self.repository_data_scraper.accumulator['file_commit_chain_scenarios']) == 0)

    def test_should_not_add_file_with_negative_counter_to_accumulator(self):
        branch = 'test_branch'
        file = 'demo_file.py'
        self.repository_data_scraper.state = {
            branch: {
                file: {
                    'file': file,
                    'branch': branch,
                    'oldest_commit': 'commit_hash_1',
                    'newest_commit': 'commit_hash_2',
                    'times_seen_consecutively': -5
                }
            }
        }

        # Should be empty before test
        self.assertTrue(len(self.repository_data_scraper.accumulator['file_commit_chain_scenarios']) == 0)

        self.repository_data_scraper.update_accumulator_with_file_commit_chain_scenario(
            self.repository_data_scraper.state[branch][file],
            file, branch)

        # Should contain the specified item after test
        self.assertTrue(len(self.repository_data_scraper.accumulator['file_commit_chain_scenarios']) == 0)


if __name__ == '__main__':
    unittest.main()
