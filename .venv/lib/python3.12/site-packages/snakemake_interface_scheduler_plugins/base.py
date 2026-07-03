__author__ = "Johannes Köster"
__copyright__ = "Copyright 2025, Johannes Köster"
__email__ = "johannes.koester@uni-due.de"
__license__ = "MIT"

import logging
from typing import Dict, Mapping, Optional, Union, Sequence
from snakemake_interface_scheduler_plugins.interfaces.dag import DAGSchedulerInterface
from snakemake_interface_scheduler_plugins.interfaces.jobs import JobSchedulerInterface
from snakemake_interface_scheduler_plugins.settings import (
    SchedulerSettingsBase,
)
from abc import ABC, abstractmethod
from snakemake_interface_common.io import AnnotatedStringInterface


class SchedulerBase(ABC):
    def __init__(
        self,
        dag: DAGSchedulerInterface,
        settings: Optional[SchedulerSettingsBase],
        logger: logging.Logger,
    ) -> None:
        self.settings: Optional[SchedulerSettingsBase] = settings
        self.logger: logging.Logger = logger
        self.dag: DAGSchedulerInterface = dag
        self.__post_init__()

    def __post_init__(self) -> None:
        pass

    def dag_updated(self) -> None:
        """This method is called when the DAG is updated.

        Use self.dag.needrun_jobs() to get an iterable of all jobs that need to be executed.
        Use self.dag.dependencies(job) to get an iterable of all dependencies of a job.
        """
        pass

    @abstractmethod
    def select_jobs(
        self,
        selectable_jobs: Sequence[JobSchedulerInterface],
        remaining_jobs: Sequence[JobSchedulerInterface],
        available_resources: Mapping[str, Union[int, str]],
        input_sizes: Dict[AnnotatedStringInterface, int],
    ) -> Optional[Sequence[JobSchedulerInterface]]:
        """Select jobs from the selectable jobs sequence. Thereby, ensure that the selected
        jobs do not exceed the available resources.

        Job resources are available via Job.scheduler_resources.

        Jobs are either single (SingleJobSchedulerInterface) or group jobs (GroupJobSchedulerInterface).
        Single jobs inside a group job can be obtained with GroupJobSchedulerInterface.jobs().

        While selecting, jobs can be given additional resources that are not
        yet defined in the job itself via Job.add_resource(name: str, value: int | str).

        The argument remaining_jobs contains all jobs that still have to be executed
        at some point, including the currently selectable jobs.

        input_sizes provides a mapping of given input files to their sizes.
        This can e.g. be used to prioritize jobs with larger input files or to weight
        the footprint of temporary files. The function uses async I/O under the hood,
        thus make sure to call it only once per job selection and collect all files of
        interest for a that single call.

        Return None to indicate an error in the selection process that shall lead to
        a fallback to the Snakemake's internal greedy scheduler.
        Otherwise, return the sequence of selected jobs.
        """
        ...
