#!/usr/bin/env python3

import json
import logging
import sys
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from statistics import median

from sou.helpers import (
    get_config,
    setup_logging,
    get_initial_input_file_sizes,
    log_predictions,
)
from sou.predictor import Predictor
from sou.snakemake_dag import SnakemakeDag
from sou.snakemake_dryrun import SnakemakeDryrun

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

    def __init__(self, name, cores, mem_mb, disk_mb):
        self.name = name
        self.cores = cores
        self.mem_mb = mem_mb
        self.disk_mb = disk_mb
        self.available_cores = cores
        self.available_mem_mb = mem_mb
        self.available_disk_mb = disk_mb

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


class Scheduler:
    def __init__(
        self,
        dag: SnakemakeDag,
        dryrun: SnakemakeDryrun,
        predictor: Predictor,
        primary_cluster: Cluster,
        secondary_cluster: Cluster,
        prices: dict,
        parallel_jobs: int = sys.maxsize,
    ):
        self.dag = dag
        self.dryrun = dryrun
        self.predictor = predictor
        self.primary_cluster = primary_cluster
        self.secondary_cluster = secondary_cluster
        self.max_parallel_jobs = parallel_jobs
        self.prices = prices  # in $, secondary cluster

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

    def predict_workflow_runtime(
        self,
        offloading_strategy: OffloadingStrategy = OffloadingStrategy.NONE,
        deadline=None,
    ):
        if offloading_strategy in [
            OffloadingStrategy.PRIMARY_OCCUPIED,
            OffloadingStrategy.NONE,
        ]:
            latest_runtime_estimate, offloaded_jobs, _ = self.simulate_workflow_run(
                offloading_strategy
            )
        else:
            latest_runtime_estimate, offloaded_jobs = (
                self.simulate_approaching_deadline(offloading_strategy, deadline)
            )
        offloaded_jobs = {
            jobid: self.dag.get_rule_by_jobid(jobid) for jobid in offloaded_jobs
        }
        return latest_runtime_estimate, offloaded_jobs

    def simulate_approaching_deadline(
        self, offloading_strategy: OffloadingStrategy, deadline: float
    ):
        if offloading_strategy == OffloadingStrategy.LONGEST_JOB_FIRST:
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

        latest_runtime_estimate, offloaded_jobs, deferred_jobs = (
            self.simulate_workflow_run(offloading_strategy)
        )
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
                        f"Offloading these jobs: {offloaded_jobs | {next_to_offload} }"
                    )
                    latest_runtime_estimate, offloaded_jobs, deferred_jobs = (
                        self.simulate_workflow_run(
                            offloading_strategy, offloaded_jobs | {next_to_offload}
                        )
                    )
                    # log_approaching_deadline(latest_runtime_estimate, len(offloaded_jobs), offloading_strategy.name)

                    if latest_runtime_estimate < minimum_runtime_estimate:
                        minimum_runtime_estimate = latest_runtime_estimate
                        offloaded_jobs_minimum_estimate = offloaded_jobs.copy()
                    else:
                        logger.warning(
                            f"Offloading job {next_to_offload} did not improve runtime estimate"
                        )

                else:
                    logger.warning("Deadline cannot be met")
                    logger.info(
                        f"Latest runtime estimate ({latest_runtime_estimate} s) was not minimal ({minimum_runtime_estimate} s), select jobs to offload according to minimum runtime estimate"
                    )
                    return minimum_runtime_estimate, offloaded_jobs_minimum_estimate
            logger.info(f"Deadline met.")
            return minimum_runtime_estimate, offloaded_jobs_minimum_estimate

    def simulate_workflow_run(
        self,
        offloading_strategy: OffloadingStrategy,
        jobs_to_offload: set[int] | None = None,
    ):
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
                    running_jobs[jobid] = self.predictions[jobid]
                    if is_offloaded:
                        logger.debug(f"Job {jobid} offloaded")
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
        return timer, all_offloaded_jobs, all_deferred_jobs

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
                    logger.warning(f"No median setup time found for job id {jobid} ({rule})")
                    predictions[jobid] = None
                    missing_jobids.append(jobid)
                    continue
                predictions[jobid] = median_setup_time + prediction
            else:  # wall time
                predictions[jobid] = prediction
        fallback_prediction = median(
            prediction for prediction in predictions.values() if prediction is not None
        ) if any(prediction is not None for prediction in predictions.values()) else 0
        for jobid in missing_jobids:
            predictions[jobid] = fallback_prediction
            logger.warning(
                f"Using fallback {model_name} prediction for job id {jobid}: {fallback_prediction}"
            )
        return predictions

    def _calculate_cost(
        self,
        runtime: float,
        requested_cores: float,
        requested_mem_mb: float,
        requested_disk_mb: float,
    ):
        return runtime * (
            (self.prices.get("vcpu_hour") / 3600) * requested_cores
            + (self.prices.get("mem_gb_hour") / 3600) * (requested_mem_mb / 1000)
            + (self.prices.get("disk_gb_hour") / 3600) * (requested_disk_mb / 1000)
        )

    def predict_offloading_cost(self, offloaded_jobs: set[int]):
        total_cost = 0
        for jobid in offloaded_jobs:
            # defaults for resources are taken from AWS Fargate: 1 vCPU, 2 GB RAM, 20 GB disk
            total_cost += self._calculate_cost(
                self.predictions[jobid],
                self.dryrun.get_requested_resources(jobid).get("threads", 1),
                self.dryrun.get_requested_resources(jobid).get("mem_mb", 2000),
                self.dryrun.get_requested_resources(jobid).get("disk_mb", 20000),
            )
        logger.info(f"Total cost of offloading: ${total_cost}")
        return total_cost


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
    return Node(name, cores, mem_mb, disk_mb)


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
    corr_threshold: float = 0.8,
):
    setup_logging()

    dryrun = SnakemakeDryrun()
    dag = SnakemakeDag()
    predictor = Predictor(runs_dir, corr_threshold)
    cluster_config = get_config().get("clusters")
    primary_cluster, secondary_cluster = create_clusters(cluster_config)
    prices = cluster_config.get("secondary", {}).get("prices", {})
    scheduler = Scheduler(
        dag, dryrun, predictor, primary_cluster, secondary_cluster, prices
    )
    runtime, offloaded_jobs = scheduler.predict_workflow_runtime(
        offloading_strategy, deadline
    )
    cost = scheduler.predict_offloading_cost(set(offloaded_jobs.keys()))

    log_predictions(runs_dir, offloading_strategy.value, runtime, offloaded_jobs, cost)
    return runtime, offloaded_jobs, cost


def run_main_safely(
    runs_dir: str,
    deadline: int,
    offloading_strategy: OffloadingStrategy,
    corr_threshold: float,
):
    try:
        return main(runs_dir, deadline, offloading_strategy, corr_threshold)
    except Exception as e:
        logger.exception("An error occurred during workflow prediction:")
