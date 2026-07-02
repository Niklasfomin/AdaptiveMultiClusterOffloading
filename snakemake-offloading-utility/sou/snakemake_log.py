import logging
import re
from datetime import datetime
from pathlib import Path

import networkx as nx

from sou.helpers import remove_storage_prefix

logger = logging.getLogger("sou")


class SnakemakeLog:
    # singleton per log file
    _instances = {}

    def __new__(cls, log_file_path):
        log_file_path = Path(log_file_path).resolve()
        if log_file_path not in cls._instances:
            instance = super().__new__(cls)
            cls._instances[log_file_path] = instance
        return cls._instances[log_file_path]

    def __init__(self, log_file_path):
        if hasattr(self, "initialized") and self.initialized:
            return

        self.log_file_path = Path(log_file_path).resolve()
        logger.info(f"Reading snakemake log '{self.log_file_path}'")
        self.log_text = self._read_log()
        self.jobs_to_files, self.benchmark_to_wall_time, self.benchmark_to_job = (
            self._parse_log()
        )
        self.initial_input_files = self._find_initial_input_files()
        self.original_dag = self._reconstruct_dag(self.jobs_to_files)
        self.initialized = True

    def _read_log(self):
        try:
            with self.log_file_path.open("r") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error reading log file {self.log_file_path}: {e}")
            raise

    def _parse_log(self):
        job_to_files = {}
        current_job_files = {}

        job_submission_times = {}
        job_start_times = {}
        job_end_times = {}
        current_timestamp = None

        file_pattern = r"[^\s()]+(?=\s*\([^()]*\)\s*(?:,|$))"

        for line in self.log_text.splitlines():
            line = line.strip()

            if timestamp_match := re.match(
                r"\[(\w{3} \w{3}  ?\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\]", line
            ):
                current_timestamp = datetime.strptime(
                    timestamp_match.group(1), "%a %b %d %H:%M:%S %Y"
                )

            elif line.startswith("rule ") or line.startswith("localrule "):
                rule = re.search(r"rule (\S+):", line).group(1)
                current_job_files = {
                    "input": [],
                    "output": [],
                    "log": [],
                    "benchmark": None,
                    "rule": rule,
                }

            elif line.startswith("input:"):
                inputs = re.findall(file_pattern, line)
                inputs = [remove_storage_prefix(f.strip()) for f in inputs]
                current_job_files["input"] = inputs

            elif line.startswith("output:"):
                outputs = re.findall(file_pattern, line)
                outputs = [remove_storage_prefix(f.strip()) for f in outputs]
                current_job_files["output"] = outputs

            elif line.startswith("log:"):
                logs = re.findall(file_pattern, line)
                logs = [remove_storage_prefix(f.strip()) for f in logs]
                current_job_files["log"] = logs

            elif line.startswith("benchmark:"):
                benchmark = re.match(r"benchmark:\s+(\S+)\s+\(", line).group(1)
                benchmark = remove_storage_prefix(benchmark.strip())
                current_job_files["benchmark"] = benchmark

            elif line.startswith("jobid:"):
                jobid = line.split(":", 1)[1].strip()
                if jobid:
                    if jobid == "81":
                        pass
                    job_to_files[jobid] = current_job_files
                    if current_timestamp:
                        if job_submission_times.get(jobid):
                            # Happens when a job is retried
                            logger.warning(
                                f"Overwriting submission time for job {jobid}"
                            )
                        job_submission_times[jobid] = current_timestamp
                    else:
                        logger.warning(f"No timestamp found for job {jobid}")
                    logger.debug(
                        f"Found job {jobid} with associated files: {current_job_files}"
                    )

            elif line.startswith("Started jobid:"):
                match = re.search(r"Started jobid: (\d+)", line)
                if match and current_timestamp:
                    jobid = match.group(1)
                    job_start_times[jobid] = current_timestamp

            elif line.startswith("Finished jobid:"):
                match = re.search(r"Finished jobid: (\d+)", line)
                if match and current_timestamp:
                    jobid = match.group(1)
                    job_end_times[jobid] = current_timestamp

        # wall time per job
        job_to_wall_time = {}
        for jobid in job_to_files.keys():
            start = job_start_times.get(jobid)
            end = job_end_times.get(jobid)
            if start and end:
                wall_time = (end - start).total_seconds()
                # wall times have only second precision and finished jobs are not always reported immediately
                # we set a minimum wall time of 1 second
                if wall_time == 0:
                    wall_time = 1
                job_to_wall_time[jobid] = wall_time
            elif not start and end:
                # there is an end time but no start time
                # this can happen if the job finished before its start was logged due to delay in reporting
                # in this case, also set wall time to 1 second
                wall_time = 1
                job_to_wall_time[jobid] = wall_time
            else:
                job_to_wall_time[jobid] = None

        # wall time per benchmark
        benchmark_to_wall_time = {}
        # jobid per benchmark
        benchmark_to_job = {}
        for jobid, wall_time in job_to_wall_time.items():
            if job_to_files[jobid].get("benchmark"):
                benchmark_filename = job_to_files[jobid]["benchmark"].split(
                    "benchmarks/"
                )[-1]
                benchmark_filename = benchmark_filename.replace("/", "_")
                benchmark_to_wall_time[benchmark_filename] = wall_time
                benchmark_to_job[benchmark_filename] = jobid

        logger.debug(f"Parsed {len(job_to_files)} job entries from log")
        # return job_to_files, benchmark_to_wall_time, rule_to_mean_wall_time
        return job_to_files, benchmark_to_wall_time, benchmark_to_job

    def _reconstruct_dag(self, jobs_to_files):
        G = nx.DiGraph()

        # Add all jobs as nodes
        for jobid, files in jobs_to_files.items():
            G.add_node(jobid)
            G.nodes[jobid]["input"] = set(files.get("input"))

        # Check for dependencies based on file overlap
        for jobid_a, files_a in jobs_to_files.items():
            for jobid_b, files_b in jobs_to_files.items():
                if jobid_a == jobid_b:
                    continue
                if set(files_a.get("output")) & set(files_b.get("input")):
                    G.add_edge(jobid_a, jobid_b)

        return G

    def _find_initial_input_files(self):
        all_inputs = set()
        all_outputs = set()

        for job in self.jobs_to_files.values():
            all_inputs.update(job.get("input", []))
            all_outputs.update(job.get("output", []))
            all_outputs.update(job.get("log", []))

        return all_inputs - all_outputs

    def get_initial_input_files(self):
        return self.initial_input_files

    def get_ancestor_input_files_by_jobid(self, jobid):
        try:
            ancestors = nx.ancestors(self.original_dag, jobid)
            ancestors.add(jobid)
            ancestor_input_files = set()
            for a in ancestors:
                ancestor_input_files.update(self.original_dag.nodes[a].get("input", {}))
            # logger.debug(f"Ancestor input files of {jobid}: {ancestor_input_files}")
            return ancestor_input_files
        except KeyError as e:
            logger.error(f"Error retrieving ancestors for jobid {jobid}: {e}")
            return set()

    def get_wall_time_by_benchmark(self, benchmark_file):
        wall_time = self.benchmark_to_wall_time.get(benchmark_file)
        if wall_time == None:
            logger.warning(f"Found no wall time for benchmark file {benchmark_file}")
        return wall_time

    def get_jobid_by_benchmark(self, benchmark_file):
        jobid = self.benchmark_to_job.get(benchmark_file)
        if not jobid:
            logger.warning(f"Found no jobid for benchmark file {benchmark_file}")
        return jobid
