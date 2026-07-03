from abc import ABC, abstractmethod
from collections import defaultdict
import logging
from typing import Dict, Iterable, List, Mapping, Optional, Type, Union
from snakemake_interface_scheduler_plugins.base import SchedulerBase
from snakemake_interface_scheduler_plugins.interfaces.dag import DAGSchedulerInterface
from snakemake_interface_scheduler_plugins.interfaces.jobs import (
    JobSchedulerInterface,
    SingleJobSchedulerInterface,
)
from snakemake_interface_scheduler_plugins.settings import SchedulerSettingsBase
from snakemake_interface_common.io import AnnotatedStringInterface


class DummyJob(JobSchedulerInterface, SingleJobSchedulerInterface):
    def __init__(
        self,
        input: List[AnnotatedStringInterface],
        output: List[AnnotatedStringInterface],
        resources: Dict[str, Union[str, int]],
    ) -> None:
        self._input = input
        self._output = output
        self._resources = resources

    @property
    def input(self) -> Iterable[AnnotatedStringInterface]:
        return self._input

    @property
    def output(self) -> Iterable[AnnotatedStringInterface]:
        return self._output

    @property
    def log(self) -> Iterable[AnnotatedStringInterface]:
        return []

    @property
    def benchmark(self) -> Iterable[AnnotatedStringInterface]:
        return []

    @property
    def priority(self) -> int:
        return 0

    @property
    def scheduler_resources(self) -> Mapping[str, Union[str, int]]:
        return self._resources

    def add_aux_resource(self, name: str, value: Union[str, int]) -> None:
        assert name not in self._resources, f"Resource {name} already exists."
        self._resources[name] = value


class DummyDAG(DAGSchedulerInterface):
    def __init__(self) -> None:
        from snakemake.io import AnnotatedString

        self._jobs = [
            DummyJob(
                input=[AnnotatedString("input1.txt")],
                output=[AnnotatedString("output1.txt")],
                resources={"cpu": 1, "mem_mb": 2048},
            ),
            DummyJob(
                input=[AnnotatedString("output1.txt")],
                output=[AnnotatedString("output2.txt")],
                resources={"cpu": 2, "mem_mb": 4096},
            ),
            DummyJob(
                input=[AnnotatedString("output1.txt")],
                output=[AnnotatedString("output3.txt")],
                resources={"cpu": 1, "mem_mb": 1024},
            ),
            DummyJob(
                input=[AnnotatedString("output1.txt")],
                output=[AnnotatedString("output4.txt")],
                resources={"cpu": 1, "mem_mb": 10024},
            ),
        ]
        self._dependencies: Mapping[
            SingleJobSchedulerInterface, List[SingleJobSchedulerInterface]
        ] = {
            self._jobs[1]: [self._jobs[0]],
            self._jobs[2]: [self._jobs[0]],
            self._jobs[3]: [self._jobs[0]],
        }
        self._finished = set()

    def jobs(self) -> Iterable[JobSchedulerInterface]:
        return self._jobs

    def job_dependencies(
        self, job: SingleJobSchedulerInterface
    ) -> Iterable[SingleJobSchedulerInterface]:
        return self._dependencies.get(job, [])

    def finished(self, job: SingleJobSchedulerInterface) -> bool:
        return job in self._finished

    def needrun_jobs(self) -> Iterable[SingleJobSchedulerInterface]:
        return (job for job in self._jobs if not self.finished(job))


class TestSchedulerBase(ABC):
    __test__ = False

    @abstractmethod
    def get_scheduler_cls(self) -> Type[SchedulerBase]: ...

    @abstractmethod
    def get_scheduler_settings(self) -> Optional[SchedulerSettingsBase]: ...

    def test_scheduler(self):
        dag = DummyDAG()
        settings = self.get_scheduler_settings()
        scheduler_cls = self.get_scheduler_cls()

        scheduler = scheduler_cls(
            dag, settings=settings, logger=logging.getLogger("TestScheduler")
        )
        assert isinstance(scheduler, SchedulerBase), (
            "Scheduler instance is not of type SchedulerBase"
        )
        assert scheduler.settings == settings, (
            "Scheduler settings do not match expected settings"
        )

        scheduler.dag_updated()

        scheduled = scheduler.select_jobs(
            [dag._jobs[0]],
            dag._jobs,
            available_resources={"cpu": 1, "mem_mb": 1024},
            input_sizes=defaultdict(int),
        )
        assert scheduled == [], (
            "Scheduler should not select jobs exceeding available resources"
        )

        scheduled = scheduler.select_jobs(
            [dag._jobs[0]],
            dag._jobs,
            available_resources={"cpu": 1, "mem_mb": 2048},
            input_sizes=defaultdict(int),
        )
        assert scheduled == [dag._jobs[0]], "Scheduler did not select the expected job"

        dag._finished.add(dag._jobs[0])

        scheduled = scheduler.select_jobs(
            [dag._jobs[1], dag._jobs[2], dag._jobs[3]],
            dag._jobs,
            available_resources={"cpu": 5, "mem_mb": 10000},
            input_sizes=defaultdict(int),
        )
        assert set(scheduled) == set([dag._jobs[1], dag._jobs[2]]), (
            "Scheduler did not select the expected jobs"
        )
