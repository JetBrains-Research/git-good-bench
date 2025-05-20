import unittest
import os
from git import Repo
from sys import path

path.append("..")
from src.repository_data_scraper.repository_data_scraper import RepositoryDataScraper
from src.repository_data_scraper.programming_language import ProgrammingLanguage


class ScrapeTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.chdir('../..')
        cls.path_to_repositories = os.path.join(os.getcwd(), 'repos', 'testing-repositories')

    def test_should_generate_target_file_commit_grams(self):
        demo_repo = Repo(os.path.join(self.path_to_repositories, 'demo-repo.git'))
        os.chdir(os.path.join(self.path_to_repositories, 'demo-repo.git'))

        self.repository_data_scraper = RepositoryDataScraper(repository=demo_repo,
                                                             programming_language=ProgrammingLanguage.TEXT,
                                                             repository_name='demo-repo',
                                                             sliding_window_size=2)

        target_file_commit_grams = [{
            'file': 'document2.txt',
            'branch': 'compliance-doc2',
            'first_commit': '464e49457108e9685e0634c9da87254a85c06c07',
            'last_commit': '533d06d3173710a8d1cb15b823cb7f9dcf72d536',
            'times_seen_consecutively': 3},
            {'file': 'document2.txt',
             'branch': 'master',
             'first_commit': 'aeeab817a1bd7d146fc7596546e0c98a0ec94dbc',
             'last_commit': '6cd3cd82bfa90b80e9435513a06014ec898de4a2',
             'times_seen_consecutively': 2},
            {'file': 'document.txt',
             'branch': 'doc-style-experimentation',
             'first_commit': 'cf99230146d4be91004b0a68c17c69ce65945ad2',
             'last_commit': '025e1062182f5ecb404767c17180310923b0f134',
             'times_seen_consecutively': 4},
            {'file': 'document2.txt',
             'branch': 'document2',
             'first_commit': '7821ce308f797eeec65da787b96c829238e15d11',
             'last_commit': '533d06d3173710a8d1cb15b823cb7f9dcf72d536',
             'times_seen_consecutively': 4}]

        self.repository_data_scraper.scrape()
        candidate_file_commit_grams = self.repository_data_scraper.accumulator['file_commit_chain_scenarios']

        self.assertEqual(len(candidate_file_commit_grams), len(target_file_commit_grams))

        for candidate_file_commit_gram in candidate_file_commit_grams:
            self.assertIn(candidate_file_commit_gram, target_file_commit_grams)

    def test_should_generate_target_file_commit_grams_in_mixed_file_type_setting(self):
        os.chdir('../..')

        demo_repo = Repo(os.path.join(self.path_to_repositories, 'strict-file-commit-grams'))
        os.chdir(os.path.join(self.path_to_repositories, 'strict-file-commit-grams'))

        self.repository_data_scraper = RepositoryDataScraper(repository=demo_repo,
                                                             programming_language=ProgrammingLanguage.PYTHON,
                                                             repository_name='demo-repo',
                                                             sliding_window_size=2)
        # File-commit gram of supported ProgrammingLanguage should NOT encompass noisy commits of other file
        # types (e.g., .md, .json, .csv, ...)
        target_file_commit_grams = [{
            'file': 'foo.py',
            'branch': 'main',
            'first_commit': '7e8a9e5487752e4500a545b0b39b60412685cd4a',
            'last_commit': '4721e6cb6f7aeb4775f975fb38a87fc0b6319c80',
            'times_seen_consecutively': 2}]

        self.repository_data_scraper.scrape()
        candidate_file_commit_grams = self.repository_data_scraper.accumulator['file_commit_chain_scenarios']

        self.assertEqual(len(candidate_file_commit_grams), len(target_file_commit_grams))

        for candidate_file_commit_gram in candidate_file_commit_grams:
            self.assertIn(candidate_file_commit_gram, target_file_commit_grams)

    def test_accumulator_should_not_contain_grams_of_invalid_file_type(self):
        demo_repo = Repo(os.path.join(self.path_to_repositories, 'mixed-file-types-demo.git'))
        os.chdir(os.path.join(self.path_to_repositories, 'mixed-file-types-demo.git'))

        # PYTHON should not contain TEXT grams
        self.repository_data_scraper = RepositoryDataScraper(repository=demo_repo,
                                                             programming_language=ProgrammingLanguage.PYTHON,
                                                             repository_name='mixed-file-types-demo',
                                                             sliding_window_size=2)

        self.repository_data_scraper.scrape()
        candidate_file_commit_grams = self.repository_data_scraper.accumulator['file_commit_chain_scenarios']

        for candidate_file_commit_gram in candidate_file_commit_grams:
            self.assertTrue(
                self.repository_data_scraper.programming_language.value in candidate_file_commit_gram['file'])

        # TEXT should not contain PYTHON grams
        self.repository_data_scraper = RepositoryDataScraper(repository=demo_repo,
                                                             programming_language=ProgrammingLanguage.TEXT,
                                                             repository_name='mixed-file-types-demo',
                                                             sliding_window_size=2)

        self.repository_data_scraper.scrape()
        candidate_file_commit_grams = self.repository_data_scraper.accumulator['file_commit_chain_scenarios']

        for candidate_file_commit_gram in candidate_file_commit_grams:
            self.assertTrue(
                self.repository_data_scraper.programming_language.value in candidate_file_commit_gram['file'])

    def test_should_generate_target_merge_scenarios(self):
        demo_repo = Repo(os.path.join(self.path_to_repositories, 'demo-repo.git'))
        os.chdir(os.path.join(self.path_to_repositories, 'demo-repo.git'))

        self.repository_data_scraper = RepositoryDataScraper(repository=demo_repo,
                                                             programming_language=ProgrammingLanguage.TEXT,
                                                             repository_name='demo-repo',
                                                             sliding_window_size=2)

        target_merge_scenarios = [
            {'merge_commit_hash': '7821ce308f797eeec65da787b96c829238e15d11', 'had_conflicts': True,
             'parents': ['618b1cb31ac5069b193603e09e3147472166b663', 'e7f94bfac56abc6a0b408fee81c018fd220030f0']},
            {'merge_commit_hash': 'aeeab817a1bd7d146fc7596546e0c98a0ec94dbc', 'had_conflicts': True,
             'parents': ['6cd3cd82bfa90b80e9435513a06014ec898de4a2', '464e49457108e9685e0634c9da87254a85c06c07']},
            {'merge_commit_hash': 'aa744a52fa0a7ee5b21007e64971dc2da7fa228a', 'had_conflicts': False,
             'parents': ['cf99230146d4be91004b0a68c17c69ce65945ad2', '618b1cb31ac5069b193603e09e3147472166b663']}]

        self.repository_data_scraper.scrape()
        candidate_merge_scenarios = self.repository_data_scraper.accumulator['merge_scenarios']

        self.assertEqual(len(candidate_merge_scenarios), len(target_merge_scenarios))

        for candidate_merge_scenario in candidate_merge_scenarios:
            self.assertIn(candidate_merge_scenario, target_merge_scenarios)

    def test_should_generate_target_cherry_pick_scenarios(self):
        demo_repo = Repo(os.path.join(self.path_to_repositories, 'mixed-file-types-demo.git'))
        os.chdir(os.path.join(self.path_to_repositories, 'mixed-file-types-demo.git'))

        self.repository_data_scraper = RepositoryDataScraper(repository=demo_repo,
                                                             programming_language=ProgrammingLanguage.TEXT,
                                                             repository_name='mixed-file-types-demo',
                                                             sliding_window_size=2)

        target_cherry_pick_scenarios = [
            # Cherry-pick scenario found via commit message pattern generated by -x
            {'cherry_pick_commit': '48baa2580692f94643332494d479a06e63f3b5cc',
             'cherry_commit': '2c8c14e9c5747385b6ce3255d65138164059c779',
             'parents': ['c469332e04959f088e0f669c254a18819b6cb791']},
            # Cherry-pick scenario found by detecing duplicate commit messages applying an identical patch
            # Note: There exists another case of duplicate commit messages,
            # where the two commits do not apply the same patch
            {'cherry_pick_commit': '5a64a9cb0e3335b4a774ff8bf72bb28def14934c',
             'cherry_commit': 'd973a53d3bb5213bc31c3008baea0e7de6b8889c',
             'parents': ['48baa2580692f94643332494d479a06e63f3b5cc']}]

        self.repository_data_scraper.scrape()
        candidate_cherry_pick_scenarios = self.repository_data_scraper.accumulator['cherry_pick_scenarios']

        self.assertEqual(len(candidate_cherry_pick_scenarios), len(target_cherry_pick_scenarios))

        for candidate_cherry_pick_scenario in candidate_cherry_pick_scenarios:
            self.assertIn(candidate_cherry_pick_scenario, target_cherry_pick_scenarios)


if __name__ == '__main__':
    unittest.main()
