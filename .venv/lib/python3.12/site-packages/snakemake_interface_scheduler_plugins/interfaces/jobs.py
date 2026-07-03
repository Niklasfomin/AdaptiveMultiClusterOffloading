from abc import ABC, abstractmethod
from typing import Mapping, Iterable, Union

from snakemake_interface_common.io import AnnotatedStringInterface


class JobSchedulerInterface(ABC):
    @property
    @abstractmethod
    def priority(self) -> int:
        """Return the priority of the job."""
        ...

    @property
    @abstractmethod
    def scheduler_resources(self) -> Mapping[str, Union[str, int]]:
        """Return a dictionary of resources used by the job."""
        ...


class SingleJobSchedulerInterface(ABC):
    @property
    @abstractmethod
    def input(self) -> Iterable[AnnotatedStringInterface]:
        """Return an iterable of input files for the job."""
        ...

    @property
    @abstractmethod
    def output(self) -> Iterable[AnnotatedStringInterface]:
        """Return an iterable of output files for the job."""
        ...

    @property
    @abstractmethod
    def log(self) -> Iterable[AnnotatedStringInterface]:
        """Return an iterable of log files for the job."""
        ...

    @property
    @abstractmethod
    def benchmark(self) -> AnnotatedStringInterface:
        """Return an iterable of benchmark files for the job."""
        ...

    @abstractmethod
    def add_aux_resource(self, name: str, value: Union[str, int]) -> None:
        """Add a resource to the job."""
        ...


class GroupJobSchedulerInterface(ABC):
    @abstractmethod
    def jobs(self) -> Iterable[SingleJobSchedulerInterface]:
        """Return an iterable of jobs in the group."""
        ...
