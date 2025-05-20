from typing import Generator, Optional

from yt.wrapper.response_stream import ResponseStream

from src.data_processing_scripts.schemas import RepositoryDataRow, SampleDataRowV4


class GitDatasetProvider:
    """
        Abstraction for interactions with the dataset. Provides functionality to iterate over the dataset and retrieve
        the scenarios of a repository.
    """

    def __init__(self, record_response_stream: ResponseStream):
        """
        Args:
            record_response_stream: Stream containing the response data from YTsaurus.
        """
        self.dataset_stream = record_response_stream
        self.current_repository: Optional[RepositoryDataRow] = None

    def stream_samples(self) -> Generator[SampleDataRowV4, None, None]:
        """
        Streams samples from the dataset.

        Returns:
            Generator: A generator for SampleDataRowV4 objects.

        Yields:
            SampleDataRowV4: A data row representing a repository from the dataset.
        """
        for sample in self.dataset_stream:
            yield sample