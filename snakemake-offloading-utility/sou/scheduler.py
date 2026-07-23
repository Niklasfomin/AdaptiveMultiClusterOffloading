#!/usr/bin/env python3

import csv
import json
import logging
import sys
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from statistics import median
from typing import cast

from sou.helpers import (
    get_config,
    get_initial_input_file_sizes,
    log_predictions,
    setup_logging,
)
from sou.predictor import Predictor
from sou.snakemake_dag import SnakemakeDag
from sou.snakemake_dryrun import SnakemakeDryrun
from sou.snakemake_log import SnakemakeLog

logger = logging.getLogger("sou")


@dataclass
class Node:
    name: str
    cores: int
    mem_mb: float
    disk_mb: float
    available_cores: int
    available_mem_mb: float
    available_disk_mb: float
    cpu_score: float | None
    io_score: float | None
    mem_score: float | None
    net_score: float | None
    contention_score: float | None
    runtime_factor: float | None
    ip_address: str

    def __init__(self, name, cores, mem_mb, disk_mb, ip_address):
        self.name = name
        self.ip_address = ip_address
        self.cores = cores
        self.mem_mb = mem_mb
        self.disk_mb = disk_mb
        self.available_cores = cores
        self.available_mem_mb = mem_mb
        self.available_disk_mb = disk_mb
        self.cpu_score = None
        self.io_score = None
        self.mem_score = None
        self.net_score = None
        self.contention_score = None
        self.runtime_factor = None

    def check_if_scheduling_possible(self, cores, mem_mb, disk_mb):
        if self.cores < cores:
            return False
        elif self.mem_mb < mem_mb:
            return False
        elif self.disk_mb < disk_mb:
            return False
        else:
            return True

    def attempt_to_schedule(self, cores, mem_mb, disk_mb):
        if (
            self.available_cores >= cores
            and self.available_mem_mb >= mem_mb
            and self.available_disk_mb >= disk_mb
        ):
            self.available_cores -= cores
            self.available_mem_mb -= mem_mb
            self.available_disk_mb -= disk_mb
            return True
        return False

    def reset_resources(self, requested_resources: dict):
        self.available_cores += requested_resources.get("threads", 1)
        self.available_mem_mb += requested_resources.get("mem_mb", 0)
        self.available_disk_mb += requested_resources.get("disk_mb", 0)


@dataclass
class Cluster:
    type: str
    nodes: list[Node]


class OffloadingStrategy(Enum):
    NONE = "No offloading"
    PRIMARY_OCCUPIED = "Primary cluster fully occupied"
    LONGEST_JOB_FIRST = "Longest job first"
    SMALLEST_INPUT_SIZE_FIRST = "Smallest input size first"

class UncertaintyAwareOffloadingStrategy(Enum):
    NONE = "No offloading"
    PRIMARY_BURST = "Fully load Primary cluster"
    LONGEST_JOB_FIRST = "Longest job first"
    CRITICAL_PATH_FIRST = "Select progress critical job first"
    SMALLEST_INPUT_SIZE_FIRST = "Data centric smallest input size first"
    LARGEST_INPUT_SIZE_FIRST = "Data centric largest input size first"
    COST_PERFORMANCE_IMPROVEMENT_FIRST = "Selection based on biggest Runtime / cost ratio improvement"

class ProfilingStrategy(Enum):
    NONE = "No profiling"
    WORFLOW_TEST = "Workflow test profile"
    SYSTEM_BENCHMARK = "System benchmark profile"


class ProfilingEnvironment(Enum):
    NONE = "No profiling"
    LOCAL = "Profiling the scientist's PC"
    REMOTE = "Profiling the one or multiple sites"


class CostModel(Enum):
    VM = "cloud vm pricing"
    GKE = "cloud cluster pricing"
    NETWORK_LATENCY = "cost incurring for data transfer between clusters"


class RuntimeEstimator(Enum):
    LINEAR_REGRESSION = "linear regression model for runtime estimation"
    BAYESIAN_REGRESSION = "probabilistic model for runtime estimation"


class Scheduler:
    def __init__(
        self,
        dag: SnakemakeDag,
        dryrun: SnakemakeDryrun,
        predictor: Predictor,
        primary_cluster: Cluster,
        secondary_cluster: Cluster,
        prices: dict,
        snakemake_log: SnakemakeLog,
        parallel_jobs: int = sys.maxsize,
    ):
        self.dag = dag
        self.dryrun = dryrun
        self.predictor = predictor
        self.primary_cluster = primary_cluster
        self.secondary_cluster = secondary_cluster
        self.max_parallel_jobs = parallel_jobs
        self.prices = prices  # in $, secondary cluster
        self.snakemake_log = snakemake_log
        self.node_benchmark_scores: dict[str, dict[str, float]] = {}
        self.node_runtime_factors: dict[str, float] = {}
        self.local_benchmark_score: dict[str, float] = {}
        self.local_to_remote_ratios: dict[str, float] = {}
        self.last_offloaded_effective_runtime_per_job: dict[int, float] = {}

        self.jobids_to_files = self.dryrun.get_job_files()
        self.dag.annotate_nodes_with_files(self.jobids_to_files)

        initial_input_files_all_jobs = list(self.dag.get_initial_input_files_all_jobs())
        self.initial_input_file_sizes = get_initial_input_file_sizes(
            initial_input_files_all_jobs
        )
        self.predictions_wall_time = self.get_time_predictions(
            "model_wall_time_apriori"
        )
        self.predictions_runtime = self.get_time_predictions("model_runtime_apriori")
        self.predictions = self.predictions_runtime

        with open(Path(".sou/latest_predictions_wall_time.json"), "w") as f:
            json.dump(self.predictions_wall_time, f, indent=4)
        with open(Path(".sou/latest_predictions_runtime.json"), "w") as f:
            json.dump(self.predictions_runtime, f, indent=4)

    @staticmethod
    def compute_contention_slowdown(contention_score: float, weight: float) -> float:
            # raw score: 1.0 = no performance loss, 0.5 = half performance remains
            c = min(max(contention_score, 0.1), 1.0)

            # weight: 0 = ignore contention, 1 = apply full contention effect
            w = min(max(weight, 0.0), 1.0)

            # transform retained performance into runtime slowdown
            return 1.0 + w * ((1.0 / c) - 1.0)

    # TODO: Add dynamic and bounded weighting to these scores, e.g. based on the workflow's resource requirements and the node's benchmark scores
    @staticmethod
    def compute_node_runtime_factors_from_csv(
        benchmark_csv: str,
    ) -> dict[str, float]:
        """
        Fastest node gets factor 1.0; slower nodes get larger factors.
        """
        def optional_float(row: dict[str, str], *names: str) -> float | None:
            for name in names:
                value = row.get(name)
                if value not in (None, ""):
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        return None
            return None

        scores: list[dict[str, str | float | None]] = []
        for row in csv.DictReader(benchmark_csv.splitlines()):
            node_name = row.get("node")
            try:
                cpu_score = float(row["cpu_score"])
                io_score = float(row["io_score"])
            except (KeyError, TypeError, ValueError):
                continue
            if not node_name or cpu_score <= 0 or io_score <= 0:
                continue
            scores.append(
                {
                    "node": node_name,
                    "cpu": cpu_score,
                    "io": io_score,
                    "mem": optional_float(row, "memory_score", "mem_score"),
                    "net": optional_float(row, "network_score_mbps", "net_score"),
                    "contention": optional_float(row, "contention_score"),
                }
            )

        if not scores:
            return {}

        maxima = {
            metric: max(
                cast(float, score[metric])
                for score in scores
                if score[metric] is not None
                and cast(float, score[metric]) > 0
            )
            for metric in ("cpu", "io")
        }
        for metric in ("mem", "net"):
            values = [
                cast(float, score[metric])
                for score in scores
                if score[metric] is not None
                and cast(float, score[metric]) > 0
            ]
            maxima[metric] = max(values) if values else 0.0

        weighted_scores: dict[str, float] = {}
        for score in scores:
            cpu = cast(float, score["cpu"]) / maxima["cpu"]
            io = cast(float, score["io"]) / maxima["io"]
            has_extended_scores = (
                score["mem"] is not None
                and score["net"] is not None
                and score["contention"] is not None
                and maxima["mem"] > 0
                and maxima["net"] > 0
            )
            if has_extended_scores:
                base_score = (
                    0.3 * cpu
                    + 0.3 * io
                    + 0.2 * (cast(float, score["mem"]) / maxima["mem"])
                    + 0.2 * (cast(float, score["net"]) / maxima["net"])
                )
                weighted_score = base_score / Scheduler.compute_contention_slowdown(
                    cast(float, score["contention"]), weight=0.5
                )
            else:
                weighted_score = 0.5 * cpu + 0.5 * io
            weighted_scores[str(score["node"])] = weighted_score

        best_score = max(weighted_scores.values())
        if best_score <= 0:
            return {}

        factors = {
            node_name: best_score / score
            for node_name, score in weighted_scores.items()
            if score > 0
        }
        logger.info("Computed node runtime factors (best=1.0): %s", factors)
        return factors

    def update_node_runtime_factors(self, benchmark_csv: str) -> dict[str, float]:
        """
        Update node benchmark scores and compute runtime multipliers.

        The fastest node gets factor 1.0; slower nodes get larger factors.
        """
        by_name = {
            node.name: node
            for node in self.primary_cluster.nodes + self.secondary_cluster.nodes
        }
        self.node_benchmark_scores = {}

        def optional_score(row: dict[str, str], *names: str) -> float | None:
            for name in names:
                value = row.get(name)
                if value not in (None, ""):
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        return None
            return None

        for row in csv.DictReader(benchmark_csv.splitlines()):
            node_name = row.get("node")
            if not node_name or node_name not in by_name:
                continue
            try:
                cpu_score = float(row["cpu_score"])
                io_score = float(row["io_score"])
            except (KeyError, TypeError, ValueError):
                continue

            node = by_name[node_name]
            node.cpu_score = cpu_score
            node.io_score = io_score
            node.mem_score = optional_score(row, "memory_score", "mem_score")
            node.net_score = optional_score(row, "network_score_mbps", "net_score")
            node.contention_score = optional_score(row, "contention_score")
            self.node_benchmark_scores[node_name] = {
                "cpu_score": cpu_score,
                "io_score": io_score,
            }

        computed_factors = self.compute_node_runtime_factors_from_csv(benchmark_csv)
        self.node_runtime_factors = {
            node_name: factor
            for node_name, factor in computed_factors.items()
            if node_name in by_name
        }
        for node_name, factor in self.node_runtime_factors.items():
            by_name[node_name].runtime_factor = factor

        logger.info(
            "Computed node runtime factors (best=1.0): %s",
            self.node_runtime_factors,
        )
        return self.node_runtime_factors

    def _get_node_of_job(self, jobid: int) -> Node | None:
        primary_nodes = self.primary_cluster.nodes
        for n in primary_nodes:
            if n.name == self.dag.get_cluster_node(jobid):
                return n
        if self.secondary_cluster:
            secondary_nodes = self.secondary_cluster.nodes
            for n in secondary_nodes:
                if n.name == self.dag.get_cluster_node(jobid):
                    return n
        logger.warning(f"Cluster node of job {jobid} not found")
        return None

    def _assign_job_to_node(
        self,
        jobid: int,
        offloading_strategy: OffloadingStrategy,
        force_offloading: bool = False,
    ) -> tuple[str | None, bool]:
        requested_resources = self.dryrun.get_requested_resources(jobid)
        requested_cores = requested_resources.get("threads", 1)
        requested_mem_mb = requested_resources.get("mem_mb", 0)
        requested_disk_mb = requested_resources.get("disk_mb", 0)

        # check if job can be scheduled in principle when all nodes are free
        if not any(
            [
                node.check_if_scheduling_possible(
                    requested_cores, requested_mem_mb, requested_disk_mb
                )
                for node in self.primary_cluster.nodes + self.secondary_cluster.nodes
            ]
        ):
            raise Exception(
                f"Job {jobid} cannot be scheduled because none of the nodes fulfill the resource requirements {requested_resources}"
            )

        if offloading_strategy in [
            OffloadingStrategy.LONGEST_JOB_FIRST,
            OffloadingStrategy.SMALLEST_INPUT_SIZE_FIRST,
        ]:
            if force_offloading:
                # must be offloaded
                for node in self.secondary_cluster.nodes:
                    if node.attempt_to_schedule(
                        requested_cores, requested_mem_mb, requested_disk_mb
                    ):
                        return node.name, True
            else:
                # must not be offloaded
                for node in self.primary_cluster.nodes:
                    if node.attempt_to_schedule(
                        requested_cores, requested_mem_mb, requested_disk_mb
                    ):
                        return node.name, False
            return None, False
        else:
            for node in self.primary_cluster.nodes:
                if node.attempt_to_schedule(
                    requested_cores, requested_mem_mb, requested_disk_mb
                ):
                    return node.name, False
            if offloading_strategy == OffloadingStrategy.PRIMARY_OCCUPIED:
                for node in self.secondary_cluster.nodes:
                    if node.attempt_to_schedule(
                        requested_cores, requested_mem_mb, requested_disk_mb
                    ):
                        return node.name, True
            return None, False

    def extrapolate_runtime_prediction(self, jobid: int, node_name: str) -> float:
        if node_name in self.node_runtime_factors:
            runtime_factor = self.node_runtime_factors[node_name]
            if runtime_factor is not None and runtime_factor > 0:
                return self.predictions[jobid] * runtime_factor
        logger.warning(
            f"Node {node_name} has no valid runtime factor, using original prediction for job {jobid}"
        )
        return self.predictions[jobid]

    # TODO: Add/change strategies here
    def predict_workflow_runtime(
        self,
        offloading_strategy: OffloadingStrategy = OffloadingStrategy.NONE,
        deadline=None,
        profiling_environment: ProfilingEnvironment = ProfilingEnvironment.NONE,
    ):
        if offloading_strategy in [
            OffloadingStrategy.PRIMARY_OCCUPIED,
            OffloadingStrategy.NONE,
        ]:
            latest_runtime_estimate, offloaded_jobs, _, offloaded_effective_runtime_per_job = self.simulate_workflow_run(
                offloading_strategy,
                profiling_environment=profiling_environment,
            )
            self.last_offloaded_effective_runtime_per_job = offloaded_effective_runtime_per_job.copy()

        else:
            latest_runtime_estimate, offloaded_jobs = (
                self.simulate_approaching_deadline(
                    offloading_strategy,
                    deadline,
                    profiling_environment=profiling_environment,
                )
            )
        offloaded_jobs = {
            jobid: self.dag.get_rule_by_jobid(jobid) for jobid in offloaded_jobs
        }
        return latest_runtime_estimate, offloaded_jobs



    def simulate_approaching_deadline(
        self,
        offloading_strategy: OffloadingStrategy,
        deadline: float,
        profiling_environment: ProfilingEnvironment = ProfilingEnvironment.NONE,
    ):
        if offloading_strategy == OffloadingStrategy.LONGEST_JOB_FIRST:
            if profiling_environment == ProfilingEnvironment.REMOTE:
                sort_deferred_func = lambda j: max(
                    (
                        self.extrapolate_runtime_prediction(j, node.name)
                        for node in self.secondary_cluster.nodes
                        if node.check_if_scheduling_possible(
                            self.dryrun.get_requested_resources(j).get("threads", 1),
                            self.dryrun.get_requested_resources(j).get("mem_mb", 0),
                            self.dryrun.get_requested_resources(j).get("disk_mb", 0),
                        )
                    ),
                    default=self.predictions[j],
                )
            else:
                sort_deferred_func = lambda j: self.predictions[j]

        elif offloading_strategy == OffloadingStrategy.SMALLEST_INPUT_SIZE_FIRST:
            sort_deferred_func = lambda j: get_total_input_size(
                self.dag.get_initial_input_files_ancestors(j),
                self.initial_input_file_sizes,
            )
        else:
            raise Exception(f"Offloading strategy {offloading_strategy} not supported")

        simulation_counter = 1
        logger.info(f"Starting simulation run {simulation_counter}")

        latest_runtime_estimate, offloaded_jobs, deferred_jobs, offloaded_effective_runtime_per_job = (
            self.simulate_workflow_run(
                offloading_strategy,
                profiling_environment=profiling_environment,
            )
        )
        offloaded_effective_runtime_minimum = offloaded_effective_runtime_per_job.copy()


        # log_approaching_deadline(latest_runtime_estimate, len(offloaded_jobs), offloading_strategy.name)

        if latest_runtime_estimate <= deadline:
            logger.info(f"Workflow can be completed without offloading")
            return latest_runtime_estimate, offloaded_jobs
        else:
            minimum_runtime_estimate = latest_runtime_estimate
            offloaded_jobs_minimum_estimate = offloaded_jobs.copy()
            while latest_runtime_estimate > deadline:
                not_yet_offloaded = deferred_jobs - offloaded_jobs
                if len(not_yet_offloaded) > 0:
                    simulation_counter += 1
                    logger.info(f"Starting simulation run {simulation_counter}")

                    next_to_offload = sorted(
                        not_yet_offloaded, key=sort_deferred_func, reverse=True
                    )[0]
                    logger.info(
                        f"Selected next job to offload: {next_to_offload} ({self.dag.get_rule_by_jobid(next_to_offload)})"
                    )
                    logger.info(
                        f"Offloading these jobs: {offloaded_jobs | {next_to_offload}}"
                    )
                    latest_runtime_estimate, offloaded_jobs, deferred_jobs, offloaded_effective_runtime_per_job = (
                        self.simulate_workflow_run(
                            offloading_strategy,
                            profiling_environment=profiling_environment,
                            jobs_to_offload=offloaded_jobs | {next_to_offload},
                        )
                    )
                    # log_approaching_deadline(latest_runtime_estimate, len(offloaded_jobs), offloading_strategy.name)

                    if latest_runtime_estimate < minimum_runtime_estimate:
                        minimum_runtime_estimate = latest_runtime_estimate
                        offloaded_jobs_minimum_estimate = offloaded_jobs.copy()
                        offloaded_effective_runtime_minimum = offloaded_effective_runtime_per_job.copy()
                    else:
                        logger.warning(
                            f"Offloading job {next_to_offload} did not improve runtime estimate"
                        )

                else:
                    logger.warning("Deadline cannot be met")
                    logger.info(
                        f"Latest runtime estimate ({latest_runtime_estimate} s) was not minimal ({minimum_runtime_estimate} s), select jobs to offload according to minimum runtime estimate"
                    )
                    self.last_offloaded_effective_runtime_per_job = offloaded_effective_runtime_minimum.copy()
                    return minimum_runtime_estimate, offloaded_jobs_minimum_estimate
            logger.info(f"Deadline met.")
            self.last_offloaded_effective_runtime_per_job = offloaded_effective_runtime_minimum.copy()

            return minimum_runtime_estimate, offloaded_jobs_minimum_estimate

    def simulate_workflow_run(
        self,
        offloading_strategy: OffloadingStrategy,
        profiling_environment: ProfilingEnvironment = ProfilingEnvironment.NONE,
        jobs_to_offload: set[int] | None = None,
    ):
        effective_runtime_per_job = {}
        offloaded_effective_runtime_per_job = {}
        job_queue = deque()
        job_queue_deferred = deque()
        running_jobs = {}
        all_offloaded_jobs = set()
        all_deferred_jobs = set()
        timer = 0

        # offloading algorithm
        while self.dag.has_uncompleted() or len(job_queue) > 0:
            logger.debug(f"Waiting jobs: {sorted(list(job_queue))}")
            logger.debug(f"Running jobs: {sorted(list(running_jobs.keys()))}")
            runnable_jobs = self.dag.get_runnable()
            if runnable_jobs:
                job_queue.extendleft(runnable_jobs)

            while len(job_queue) > 0 and len(running_jobs) <= self.max_parallel_jobs:
                jobid = job_queue.pop()
                force_offloading = (
                    False if not jobs_to_offload else jobid in jobs_to_offload
                )
                node, is_offloaded = self._assign_job_to_node(
                    jobid, offloading_strategy, force_offloading
                )
                if node:
                    logger.debug(f"Job {jobid} -> {node}")
                    self.dag.set_cluster_node(jobid, node)
                    # TODO: Here is where extrapolation based on node performance ratios is applied to the runtime prediction
                    effective = (self.extrapolate_runtime_prediction(jobid, node)
                        if profiling_environment == ProfilingEnvironment.REMOTE
                        else self.predictions[jobid]
                    )
                    running_jobs[jobid] = effective
                    effective_runtime_per_job[jobid] = effective
                    if is_offloaded:
                        logger.debug(f"Job {jobid} offloaded")
                        offloaded_effective_runtime_per_job[jobid] = effective
                        all_offloaded_jobs.add(jobid)
                else:
                    logger.debug(f"No node available for job {jobid}, defer...")
                    all_deferred_jobs.add(jobid)
                    job_queue_deferred.appendleft(jobid)

            job_queue.extendleft(job_queue_deferred)
            job_queue_deferred = deque()

            if running_jobs:
                time_passed = min(running_jobs.values())  # least remaining runtime
                # multiple jobs might finish at the same time if they have the same runtime prediction
                finished_jobs = {
                    jobid for jobid, time in running_jobs.items() if time == time_passed
                }
                timer += time_passed
                running_jobs = {
                    jobid: time - time_passed for jobid, time in running_jobs.items()
                }
                for jobid in finished_jobs:
                    logger.debug(f"Job {jobid} finished after {timer:.3f} s")
                    running_jobs.pop(jobid)
                    self.dag.mark_completed(jobid)

                    # reset resource utilization of node
                    node = self._get_node_of_job(jobid)
                    requested_resources_job = self.dryrun.get_requested_resources(jobid)
                    node.reset_resources(requested_resources_job)

        self.dag.reset()
        logger.info(f"Workflow simulation run completed, overall time: {timer} s")
        self.last_offloaded_effective_runtime_per_job = offloaded_effective_runtime_per_job.copy()
        return timer, all_offloaded_jobs, all_deferred_jobs, offloaded_effective_runtime_per_job

    def get_time_predictions(self, model_name: str):
        predictions = {}
        missing_jobids = []
        for jobid in self.dag.get_jobids():
            # all is not a typical rule but just used to define the desired output files
            if self.dag.job_has_label(jobid, "all"):
                continue
            rule = self.dag.get_rule_by_jobid(jobid)
            initial_input_files_ancestors = self.dag.get_initial_input_files_ancestors(
                jobid
            )
            total_initial_input_size_ancestors = get_total_input_size(
                initial_input_files_ancestors, self.initial_input_file_sizes
            )
            prediction = self.predictor.predict(
                model_name, rule, total_initial_input_size_ancestors
            )
            if prediction is None:
                logger.warning(f"No prediction for job id {jobid} ({rule})")
                predictions[jobid] = None
                missing_jobids.append(jobid)
                continue
            if "runtime" in model_name:
                median_setup_time = self.predictor.median_setup_times.get(rule)
                if median_setup_time is None:
                    logger.warning(
                        f"No median setup time found for job id {jobid} ({rule})"
                    )
                    predictions[jobid] = None
                    missing_jobids.append(jobid)
                    continue
                predictions[jobid] = median_setup_time + prediction
            else:  # wall time
                predictions[jobid] = prediction
        fallback_prediction = (
            median(
                prediction
                for prediction in predictions.values()
                if prediction is not None
            )
            if any(prediction is not None for prediction in predictions.values())
            else 0
        )
        for jobid in missing_jobids:
            predictions[jobid] = fallback_prediction
            logger.warning(
                f"Using fallback {model_name} prediction for job id {jobid}: {fallback_prediction}"
            )
        return predictions

    # TODO: It might still operate on base runtimes and not on extrapolated runtimes, which would be more accurate
    # Refactored to represent GKE Autopilot pricing model, with one-demand pricing and on-demand pod's.
    def _calculate_cost(
        self,
        runtime: float,
        requested_cores: float,
        requested_mem_mb: float,
        requested_disk_mb: float,
        transferred_output_mb: float,
    ):
        return runtime * (
            (self.prices.get("vcpu_hour") / 3600) * requested_cores
            + (self.prices.get("mem_gb_hour") / 3600) * (requested_mem_mb / 1000)
            + (self.prices.get("disk_gb_hour") / 3600) * (requested_disk_mb / 1000)
            + (self.prices.get("net_gb_hour") / 3600) * (transferred_output_mb / 1000)
        )
    # def _calculate_cost(
    #     self,
    #     runtime: float,
    #     requested_cores: float,
    #     requested_mem_mb: float,
    #     requested_disk_mb: float,
    # ):
    #     return runtime * (
    #         (self.prices.get("vcpu_hour") / 3600) * requested_cores
    #         + (self.prices.get("mem_gb_hour") / 3600) * (requested_mem_mb / 1000)
    #         + (self.prices.get("disk_gb_hour") / 3600) * (requested_disk_mb / 1000)
    #     )

    # Modified to match GKE Autopilot pricing model, with one-demand pricing and on-demand pod's.
    def predict_offloading_cost(self, offloaded_jobs: set[int]):
        total_cost = 0
        workflow_cost = 0
        primary_cluster_operational_cost = 0
        for jobid in offloaded_jobs:
            runtime = self.last_offloaded_effective_runtime_per_job.get(jobid, self.predictions[jobid])
            workflow_cost += self._calculate_cost(
                runtime if runtime is not None else 0.0,

                self.dryrun.get_requested_resources(jobid).get("threads", 1),
                self.dryrun.get_requested_resources(jobid).get("mem_mb", 2000),
                self.dryrun.get_requested_resources(jobid).get("disk_mb", 20000),
                # TODO: Insert helper method here that reads from the logs the output f
                transferred_output_mb=(
                    self.snakemake_log.get_output_size_mb_by_jobid(jobid) or 0.0
                )

            )
            total_cost += workflow_cost + primary_cluster_operational_cost
        logger.info(f"Workflow cost: ${workflow_cost}")
        logger.info(f"Total cost of offloading: ${total_cost}")
        return workflow_cost,total_cost
    # def predict_offloading_cost(self, offloaded_jobs: set[int]):
    #     total_cost = 0
    #     for jobid in offloaded_jobs:
    #         runtime = self.last_offloaded_effective_runtime_per_job.get(jobid, self.predictions[jobid])
    #         # defaults for resources are taken from AWS Fargate: 1 vCPU, 2 GB RAM, 20 GB disk
    #         total_cost += self._calculate_cost(
    #             runtime if runtime is not None else 0.0,

    #             self.dryrun.get_requested_resources(jobid).get("threads", 1),
    #             self.dryrun.get_requested_resources(jobid).get("mem_mb", 2000),
    #             self.dryrun.get_requested_resources(jobid).get("disk_mb", 20000),
    #         )
    #     logger.info(f"Total cost of offloading: ${total_cost}")
    #     return total_cost


def get_total_input_size(input_files, input_file_sizes):
    total_size = 0
    for input_file in input_files:
        if input_file in input_file_sizes:
            total_size += input_file_sizes[input_file]
        else:
            logger.warning(f"Input file {input_file} not found in initial input files")
    return total_size


def create_node(node_config: dict) -> Node:
    name = node_config["name"]
    cores = node_config["cores"]
    mem_mb = node_config["mem_gb"] * 1000
    disk_mb = node_config["disk_gb"] * 1000
    ip_address = node_config["ip_address"]
    return Node(name, cores, mem_mb, disk_mb, ip_address)


def create_clusters(cluster_config: dict) -> tuple[Cluster, Cluster]:
    logger.info(cluster_config)
    primary_nodes = cluster_config["primary"]["nodes"]
    secondary_nodes = cluster_config["secondary"]["nodes"]
    primary_cluster = Cluster("primary", [create_node(node) for node in primary_nodes])
    secondary_cluster = Cluster(
        "secondary", [create_node(node) for node in secondary_nodes]
    )
    return primary_cluster, secondary_cluster


def main(
    runs_dir: str,
    deadline: int | None = None,
    offloading_strategy: OffloadingStrategy = OffloadingStrategy.NONE,
    profiling_environment: ProfilingEnvironment = ProfilingEnvironment.NONE,
    corr_threshold: float = 0.8,
    benchmark_csv: str | None = None,
    decision_model: str = "linear",
):
    setup_logging()

    dryrun = SnakemakeDryrun()
    dag = SnakemakeDag()
    snakemake_log = SnakemakeLog("snakemake.log")
    predictor = Predictor(runs_dir, corr_threshold, decision_model)
    cluster_config = get_config().get("clusters")
    primary_cluster, secondary_cluster = create_clusters(cluster_config)
    prices = cluster_config.get("secondary", {}).get("prices", {})
    scheduler = Scheduler(
        dag, dryrun, predictor, primary_cluster, secondary_cluster, prices, snakemake_log
    )

    if profiling_environment == ProfilingEnvironment.REMOTE:
        if benchmark_csv:
            scheduler.update_node_runtime_factors(benchmark_csv)
        else:
            logger.warning(
                "Profiling environment is REMOTE but no benchmark CSV was provided; falling back to base predictions."
            )

    runtime, offloaded_jobs = scheduler.predict_workflow_runtime(
        offloading_strategy,
        deadline,
        profiling_environment=profiling_environment,
    )
    cost = scheduler.predict_offloading_cost(set(offloaded_jobs.keys()))

    log_predictions(
        runs_dir,
        offloading_strategy.value,
        runtime,
        offloaded_jobs,
        cost,
        profiling_environment.value,
    )
    return runtime, offloaded_jobs, cost


def run_main_safely(
    runs_dir: str,
    deadline: int | None,
    offloading_strategy: OffloadingStrategy,
    profiling_environment: ProfilingEnvironment,
    corr_threshold: float,
    benchmark_csv: str | None = None,
    decision_model: str = "linear",
):
    try:
        return main(
            runs_dir,
            deadline,
            offloading_strategy,
            profiling_environment,
            corr_threshold,
            benchmark_csv,
            decision_model,
        )
    except Exception as e:
        logger.exception("An error occurred during workflow prediction:")
