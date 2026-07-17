import ast
import logging
import shutil
from collections import defaultdict
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

from sou.helpers import remove_storage_prefix
from sou.snakemake_dag import SnakemakeDag
from sou.snakemake_log import SnakemakeLog

logger = logging.getLogger("sou")


@dataclass
class Datapoint:  # one datapoint per job
    filepath: Path
    jobid: int = None
    runtime: float = (
        None  # runtime of the process encapsulated by Snakemake job, as in benchmark file
    )
    wall_time: float = (
        None  # wall time of the Snakemake job, from submission to completion, as in log file
    )
    total_input_size: float = None
    input_size_dict: dict[str, float] = None
    total_initial_input_size_ancestors: float = None


@dataclass
class Run:
    run_name: str
    run_path: Path
    rules: dict[str, list[Datapoint]]


def collect_benchmark_files_of_all_runs(base_path: Path) -> list[Run]:
    runs = []
    logger.info(f"Analyzing files in: '{base_path}'")
    for run_path in base_path.iterdir():
        if not run_path.is_dir():
            continue
        benchmarks_path = run_path / "benchmarks"
        if not benchmarks_path.exists():
            logger.warning(f"No 'benchmarks' directory in {run_path}")
            continue
        preprocess_benchmarks_dir(benchmarks_path)

        run = Run(run_name=run_path.name, run_path=run_path, rules=defaultdict(list))
        for bm_file in benchmarks_path.iterdir():
            if bm_file.is_dir() or not bm_file.name.endswith(".tsv"):
                continue
            if bm_file.name == "_copied":
                continue

            # Historical naming: <rule>+<wildcards>.tsv
            if "+" in bm_file.name:
                rule_name = bm_file.name.split("+")[0]
            else:
                # Newer flattened naming (e.g. rules_star_align_T01_1.tsv).
                # We will resolve the final rule robustly in collect_benchmarks_per_rule.
                rule_name = "unknown"

            run.rules[rule_name].append(Datapoint(bm_file))

        runs.append(run)
    return runs


def collect_benchmarks_per_rule(runs: list[Run]):
    for run in runs:
        logger.info(f"Reading benchmark files from '{run.run_path}'")
        snakemake_log = SnakemakeLog(run.run_path / "snakemake.log")

        resolved_rules = defaultdict(list)
        for fallback_rule_name, datapoints in run.rules.items():
            for dp in datapoints:
                try:
                    runtime, total_input_size, input_size_dict, benchmark_rule_name = (
                        process_benchmark_file(dp.filepath)
                    )
                    dp.runtime = runtime
                    dp.total_input_size = total_input_size
                    dp.input_size_dict = {
                        remove_storage_prefix(k): v for k, v in input_size_dict.items()
                    }
                    dp.wall_time = snakemake_log.get_wall_time_by_benchmark(
                        dp.filepath.name
                    )
                    # due to a snakemake bug, the jobid in the benchmark file is always set to 0
                    dp.jobid = snakemake_log.get_jobid_by_benchmark(dp.filepath.name)

                    rule_from_log = None
                    if dp.jobid and snakemake_log.jobs_to_files.get(dp.jobid):
                        rule_from_log = snakemake_log.jobs_to_files[dp.jobid].get("rule")

                    resolved_rule_name = (
                        benchmark_rule_name or rule_from_log or fallback_rule_name
                    )
                    resolved_rules[resolved_rule_name].append(dp)
                except Exception as e:
                    logger.error(f"Error processing benchmark file {dp.filepath}: {e}")

        run.rules = resolved_rules


def compute_ancestor_input_sizes(runs: list[Run]):
    for run in runs:
        snakemake_log = SnakemakeLog(run.run_path / "snakemake.log")
        initial_input_files_all_jobs = snakemake_log.get_initial_input_files()

        # first find all input files sizes  of this run
        input_file_sizes = {}
        for rule_name, datapoints in run.rules.items():
            for dp in datapoints:
                input_file_sizes |= dp.input_size_dict

        # then compute the total initial input size of the ancestors
        for rule_name, datapoints in run.rules.items():
            for dp in datapoints:
                ancestor_input_files = snakemake_log.get_ancestor_input_files_by_jobid(
                    dp.jobid
                )
                initial_ancestor_input_files = (
                    ancestor_input_files & initial_input_files_all_jobs
                )
                total_initial_input_size_ancestors = 0
                for file in initial_ancestor_input_files:
                    size = input_file_sizes.get(file)
                    if size is None:
                        logger.warning(
                            f"Input file '{file}' not found in input_file_sizes"
                        )
                    total_initial_input_size_ancestors += size
                dp.total_initial_input_size_ancestors = (
                    total_initial_input_size_ancestors
                )


def get_median_setup_time_per_rule(runs: list[Run]) -> dict[str, float]:
    setup_times_per_rule = defaultdict(list)
    for run in runs:
        for rule_name, datapoints in run.rules.items():
            for dp in datapoints:
                try:
                    setup_time = dp.wall_time - dp.runtime
                    if setup_time < 0:
                        logger.warning(
                            f"Negative setup time for rule '{rule_name}' in run '{run.run_name}': {setup_time:.2f}s"
                        )
                        setup_time = 0
                    setup_times_per_rule[rule_name].append(setup_time)
                except Exception as e:
                    logger.error(f"Error calculating median setup time: {e}")
    median_setup_time_per_rule = {
        rule: float(np.median(times)) for rule, times in setup_times_per_rule.items()
    }
    return median_setup_time_per_rule


def get_rule_ancestors(rule_name):
    snakemake_dag = SnakemakeDag()
    return snakemake_dag.get_ancestors_by_rule(rule_name)


def get_datapoints_by_rule(runs: list[Run]) -> dict[str, list[Datapoint]]:
    all_datapoints_by_rule = defaultdict(list)
    for run in runs:
        for rule_name, datapoints in run.rules.items():
            all_datapoints_by_rule[rule_name].extend(datapoints)
    return all_datapoints_by_rule


def clean_tsv_newlines(file_path: str):
    # due to a bug in Snakemake, some benchmark files have newlines in the middle of the file as part of the params
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    newline_indices = [i for i, c in enumerate(content) if c == "\n"]

    if len(newline_indices) <= 2:
        return content

    first_nl = newline_indices[0]
    last_nl = newline_indices[-1]
    content_no_newlines = content.replace("\n", "")
    cleaned_content = (
        content_no_newlines[:first_nl]
        + "\n"
        + content_no_newlines[first_nl : last_nl - (len(newline_indices) - 2)]
        + "\n"
        + content_no_newlines[last_nl - (len(newline_indices) - 2) :]
    )
    return cleaned_content


def process_benchmark_file(filepath):
    content = clean_tsv_newlines(filepath)
    df = pd.read_csv(StringIO(content), sep="\t")
    if "datavzrd" in filepath.name:
        # Remove newline characters from all string cells
        pass
    runtime = float(df["s"].iloc[0])
    input_size_dict = ast.literal_eval(df["input_size_mb"].iloc[0])
    total_input_size = sum(input_size_dict.values())

    rule_name = None
    if "rule_name" in df.columns:
        candidate = df["rule_name"].iloc[0]
        if not pd.isna(candidate):
            candidate = str(candidate).strip()
            if candidate:
                rule_name = candidate

    return runtime, total_input_size, input_size_dict, rule_name


def preprocess_benchmarks_dir(benchmark_path: Path):
    # When slashes are part of wildcards, the benchmark directory will include subdirectories
    # Flatten this structure by copying the files with a flattened name to the benchmark directory
    base_path = Path(benchmark_path).resolve()
    if any(d for d in base_path.iterdir() if d.is_dir() and d.name != "_copied"):
        logger.info(f"Flattening benchmark subdirectories")

        # directory to move the original subdirectories to in the end
        copied_dir = base_path / "_copied"
        copied_dir.mkdir(exist_ok=True)

        for directory in [
            d for d in base_path.iterdir() if d.is_dir() and d.name != "_copied"
        ]:
            logger.info(f"Flattening subdirectory: {directory}")

            # all files in the subdirectory, also recursively
            for file_path in directory.rglob("*"):
                if file_path.is_file():
                    relative_parts = file_path.relative_to(base_path).parts
                    new_filename = "_".join(relative_parts)
                    new_path = base_path / new_filename

                    logger.debug(f"Copying '{file_path}' to '{new_path}'")
                    shutil.copy2(file_path, new_path)

            # Move the original directory to _copied
            destination = copied_dir / directory.name
            logger.debug(f"Moving directory '{directory}' to '{destination}'")
            shutil.move(str(directory), str(destination))
