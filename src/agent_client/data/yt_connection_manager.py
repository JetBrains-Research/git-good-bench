import yt.wrapper as yt
from yt.wrapper.response_stream import ResponseStream
from yt.wrapper.schema import TableSchema

from src.data_processing_scripts.schemas import SampleDataRowV4


class YTConnectionManager:
    """
    Orchestration class for communicating with the YTsaurus Map-Reduce platform where the dataset is stored.
    """

    def __init__(self, dataset_table_location: str):
        self.dataset_table_location = dataset_table_location
        self.dataset_table_path = self._fetch_dataset_table_path()

    def _fetch_dataset_table_path(self) -> yt.TablePath:
        """
        Sets up the yt TablePath object at the dataset table location.

        Returns:
            (yt.TablePath) The yt.TablePath object at the dataset table location at self.dataset_table_location.
        """
        return yt.TablePath(
            self.dataset_table_location,
            schema=TableSchema.from_row_type(SampleDataRowV4)
        )

    def get_dataset_stream(self) -> ResponseStream:
        """
        Reads a structured table from the dataset table path in a streamed manner. Each item in the iterable stream
        is a scenario sample

        Returns:
            (yt.ResponseStream): Data stream of type 'SampleDataRowV4' from the specified dataset table path.
        """
        return yt.read_table_structured(table=self.dataset_table_path, row_type=SampleDataRowV4)
