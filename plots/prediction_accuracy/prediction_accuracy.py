import argparse
import ast
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
import seaborn as sns
from scipy.stats import norm, pearsonr, spearmanr
from sklearn.linear_model import BayesianRidge
from sklearn.metrics import PredictionErrorDisplay

logger = logging.getLogger()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOU_INPUT_MODE = "ancestor"  # aligns with SOU scheduler predictions (apriori models)
REPO_ROOT = Path(__file__).resolve().parents[2]
SOU_PATH = REPO_ROOT / "snakemake-offloading-utility"
if str(SOU_PATH) not in sys.path:
    sys.path.insert(0, str(SOU_PATH))

from sou.snakemake_benchmarks import (  # noqa: E402
    collect_benchmark_files_of_all_runs,
    collect_benchmarks_per_rule,
    compute_ancestor_input_sizes,
)


def remove_storage_prefix(path: str) -> str:
    """Normalize path strings from benchmark/log records.

    Handles both historical remote-storage paths and current local absolute/relative
    paths without emitting warnings for normal local paths.
    """
    if path is None:
        return path

    p = path.strip()

    # Historical Snakemake log formatting occasionally appends notes like
    # "(send to storage)". Remove if present.
    p = re.sub(r"\s*\([^()]*\)\s*$", "", p)

    # Snakemake local storage cache prefix.
    p = re.sub(r"^\.snakemake/storage/[^/]+/", "", p)

    # URI scheme prefix (ftp://, s3://, file://, ...).
    p = re.sub(r"^\w+://", "", p)

    return p


def read_log(log_file_path):
    try:
        with open(log_file_path, "r") as f:
            return f.read()
    except Exception as e:
        logger.error(f"Error reading log file {log_file_path}: {e}")
        raise


def parse_log(log_text):
    job_to_files = {}
    current_job_files = {}

    job_submission_times = {}
    job_start_times = {}
    job_end_times = {}
    current_timestamp = None

    def _parse_paths(line, prefix):
        payload = line[len(prefix) :].strip()
        if not payload:
            return []
        items = []
        for part in payload.split(","):
            part = part.strip()
            if not part:
                continue
            # remove trailing notes like "(send to storage)" used in older logs
            part = re.sub(r"\s*\([^()]*\)\s*$", "", part).strip()
            if not part:
                continue
            items.append(remove_storage_prefix(part))
        return items

    def _parse_single_path(line, prefix):
        payload = line[len(prefix) :].strip()
        if not payload:
            return None
        payload = re.sub(r"\s*\([^()]*\)\s*$", "", payload).strip()
        if not payload:
            return None
        return remove_storage_prefix(payload)

    for line in log_text.splitlines():
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
            current_job_files["input"] = _parse_paths(line, "input:")

        elif line.startswith("output:"):
            current_job_files["output"] = _parse_paths(line, "output:")

        elif line.startswith("log:"):
            current_job_files["log"] = _parse_paths(line, "log:")

        elif line.startswith("benchmark:"):
            current_job_files["benchmark"] = _parse_single_path(line, "benchmark:")

        elif line.startswith("jobid:"):
            jobid = line.split(":", 1)[1].strip()
            if jobid:
                job_to_files[jobid] = current_job_files
                if current_timestamp:
                    if job_submission_times.get(jobid):
                        # Happens when a job is retried
                        logger.warning(f"Overwriting submission time for job {jobid}")
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
            job_to_wall_time[jobid] = (end - start).total_seconds()
        else:
            job_to_wall_time[jobid] = None

    return job_to_wall_time, job_to_files


def read_latest_predictions(path):
    with open(path) as f:
        predictions = json.load(f)
    return predictions


def calculate_differences(job_to_wall_time, predictions):
    differences = {}
    differences_rel = {}
    for jobid, wall_time in job_to_wall_time.items():
        # if jobid == "0":
        #     continue
        if jobid in predictions:
            predicted_time = predictions[jobid]
            if predicted_time is not None and wall_time is not None:
                differences[jobid] = predicted_time - wall_time
                differences_rel[jobid] = (predicted_time - wall_time) / wall_time
            else:
                logger.warning(
                    f"None value for job {jobid} in predictions or wall time"
                )
                differences[jobid] = None
        else:
            logger.warning(f"Job {jobid} has no prediction")
            differences[jobid] = None
    return differences, differences_rel


def plot_differences_relative(data, median_rules=None, workflow=None):
    # median_rules: list of rule names that should be in category 'Median'
    # workflow: string, used as plot title
    if median_rules is None:
        median_rules = []
    # Calculate variance for each rule
    rule_variances = {rule: pd.Series(values).var() for rule, values in data.items()}
    # Sort rules by variance descending
    sorted_rules = sorted(rule_variances, key=rule_variances.get, reverse=True)

    # Print quartiles and outliers for 'varlociraptor_preprocess' and omit from plot
    rows = []
    for rule in sorted_rules:
        values = data[rule]
        category = "Median" if rule in median_rules else "Linear model"
        # Print median for each rule
        median_val = pd.Series(values).median()
        print(f"Median for rule '{rule}': {median_val:.3f}")
        if rule == "varlociraptor_preprocess":
            # Print scientific characterization of the distribution
            s = pd.Series(values)
            mean = s.mean()
            std = s.std()
            median = s.median()
            q1 = s.quantile(0.25)
            q3 = s.quantile(0.75)
            skew = s.skew()
            kurt = s.kurtosis()
            min_val = s.min()
            max_val = s.max()
            n = len(s)
            print(
                f"varlociraptor_preprocess: n={n}, mean={mean:.3f}, std={std:.3f}, median={median:.3f}, Q1={q1:.3f}, Q3={q3:.3f}, min={min_val:.3f}, max={max_val:.3f}, skewness={skew:.3f}, kurtosis={kurt:.3f}"
            )
            if abs(skew) > 1:
                print(
                    f"The distribution is highly {'right' if skew > 0 else 'left'}-skewed."
                )
            elif abs(skew) > 0.5:
                print(
                    f"The distribution is moderately {'right' if skew > 0 else 'left'}-skewed."
                )
            else:
                print("The distribution is approximately symmetric.")
            if kurt > 1:
                print("The distribution is leptokurtic (heavy tails).")
            elif kurt < -1:
                print("The distribution is platykurtic (light tails).")
            else:
                print("The distribution has approximately normal tail weight.")
            continue
        for v in values:
            rows.append({"Rule": rule, "Value": v * 100, "Category": category})
    df = pd.DataFrame(rows)

    # sns.set_style("whitegrid")
    palette = {"Median": "#FF9900", "Linear model": "#1F77B4"}  # orange, blue
    # Set wider figure for dna-seq-varlociraptor, default for others
    if workflow == "dna-seq-varlociraptor":
        plt.figure(figsize=(10, 7))
    elif workflow == "stained-glass":
        plt.figure(figsize=(5, 4))
    else:
        plt.figure(figsize=(7, 5))
    # Remove 'varlociraptor_preprocess' from x-axis order for plotting
    plot_rules = [rule for rule in sorted_rules if rule != "varlociraptor_preprocess"]
    ax = sns.boxplot(
        x="Rule", y="Value", hue="Category", data=df, palette=palette, order=plot_rules
    )
    # Set smaller font for dna-seq-varlociraptor
    if workflow == "dna-seq-varlociraptor":
        plt.xticks(rotation=60, ha="right", fontsize=10)
        ax.set_ylabel("Prediction Error (%)", fontsize=14)
        ax.set_xlabel("Task", fontsize=13)
        ax.legend(title="Prediction type", fontsize=13, title_fontsize=13)
        plt.title(workflow, fontsize=16)
    else:
        plt.xticks(rotation=60, ha="right")
        if workflow:
            plt.title(workflow)
        plt.ylabel("Prediction error (%)")
        ax.set_xlabel("Task")
    # Color x-axis tick labels according to their category
    for tick_label in ax.get_xticklabels():
        rule = tick_label.get_text()
        color = palette["Median"] if rule in median_rules else palette["Linear model"]
        tick_label.set_color(color)
    plt.tight_layout()
    plt.grid(True, color="lightgray", linestyle="--", linewidth=0.5)
    # Set legend title for non-dna plots
    if workflow != "dna-seq-varlociraptor":
        handles, labels = ax.get_legend_handles_labels()
        ax.legend(handles, labels, title="Prediction type")
    plt.savefig(
        f"job_runtime_prediction_relative_{workflow}.png", dpi=300, bbox_inches="tight"
    )
    plt.savefig(f"job_runtime_prediction_relative_{workflow}.pdf", bbox_inches="tight")
    plt.show()


def get_initial_input_files(jobs_to_files):
    all_inputs = set()
    all_outputs = set()
    for files in jobs_to_files.values():
        all_inputs.update(files.get("input", []))
        all_outputs.update(files.get("output", []))
        all_outputs.update(files.get("log", []))
    return all_inputs - all_outputs


def reconstruct_dag(jobs_to_files):
    graph = nx.DiGraph()
    for jobid, files in jobs_to_files.items():
        graph.add_node(jobid, input=set(files.get("input", [])))
    for source, source_files in jobs_to_files.items():
        for target, target_files in jobs_to_files.items():
            if source != target and set(source_files.get("output", [])) & set(
                target_files.get("input", [])
            ):
                graph.add_edge(source, target)
    return graph


def read_benchmark_record(path):
    df = pd.read_csv(path, sep="\t")
    runtime = float(df["s"].iloc[0])
    input_sizes = ast.literal_eval(df["input_size_mb"].iloc[0])
    input_sizes = {remove_storage_prefix(k): v for k, v in input_sizes.items()}

    rule_name = None
    if "rule_name" in df.columns:
        candidate = df["rule_name"].iloc[0]
        if not pd.isna(candidate):
            candidate = str(candidate).strip()
            if candidate:
                rule_name = candidate

    return runtime, input_sizes, rule_name


def read_benchmark_input_sizes(path):
    runtime, input_sizes, _ = read_benchmark_record(path)
    return runtime, input_sizes


def _normalize_benchmark_ref(benchmark_ref):
    if not benchmark_ref:
        return None

    ref = remove_storage_prefix(benchmark_ref).replace("\\", "/").strip()
    if "benchmarks/" in ref:
        ref = ref.split("benchmarks/", 1)[1]
    ref = ref.lstrip("./")
    return ref or None


def resolve_benchmark_file_path(benchmark_dir, benchmark_ref):
    rel_ref = _normalize_benchmark_ref(benchmark_ref)
    if not rel_ref:
        return None

    rel_no_ext = os.path.splitext(rel_ref)[0]
    candidates = []

    for rel in (rel_ref, rel_no_ext):
        if not rel:
            continue

        if os.path.splitext(rel)[1]:
            candidates.append(os.path.join(benchmark_dir, rel))
        else:
            candidates.append(os.path.join(benchmark_dir, f"{rel}.tsv"))

        flattened = rel.replace("/", "_")
        if os.path.splitext(flattened)[1]:
            candidates.append(os.path.join(benchmark_dir, flattened))
        else:
            candidates.append(os.path.join(benchmark_dir, f"{flattened}.tsv"))

        base = os.path.basename(rel)
        if os.path.splitext(base)[1]:
            candidates.append(os.path.join(benchmark_dir, base))
        else:
            candidates.append(os.path.join(benchmark_dir, f"{base}.tsv"))

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.exists(candidate):
            return candidate

    return None


def iter_benchmark_tsv_paths(benchmark_dir):
    benchmark_paths = []
    for root, _, files in os.walk(benchmark_dir):
        for filename in files:
            if filename.endswith(".tsv"):
                benchmark_paths.append(os.path.join(root, filename))
    return sorted(benchmark_paths)


def build_benchmark_rule_lookup(log_path):
    lookup = defaultdict(set)
    if not os.path.exists(log_path):
        return lookup

    _, jobs_to_files = parse_log(read_log(log_path))
    for files in jobs_to_files.values():
        benchmark_ref = files.get("benchmark")
        rule = files.get("rule")
        rel_ref = _normalize_benchmark_ref(benchmark_ref)
        if not rel_ref or not rule:
            continue

        rel_stem = os.path.splitext(rel_ref)[0]
        candidates = {
            rel_ref,
            rel_stem,
            rel_ref.replace("/", "_"),
            rel_stem.replace("/", "_"),
            os.path.basename(rel_ref),
            os.path.basename(rel_stem),
        }
        for candidate in candidates:
            if candidate:
                lookup[candidate].add(rule)

    return lookup


def _lookup_unique_rule(rule_lookup, key):
    rules = rule_lookup.get(key)
    if not rules or len(rules) != 1:
        return None
    return next(iter(rules))


def infer_rule_for_benchmark(benchmark_path, benchmark_dir, rule_name=None, rule_lookup=None):
    if rule_name:
        return rule_name

    if rule_lookup is None:
        rule_lookup = {}

    rel = os.path.relpath(benchmark_path, benchmark_dir).replace("\\", "/")
    rel_stem = os.path.splitext(rel)[0]
    basename = os.path.basename(rel)
    stem = os.path.splitext(basename)[0]

    keys = (
        rel,
        rel_stem,
        rel.replace("/", "_"),
        rel_stem.replace("/", "_"),
        basename,
        stem,
    )
    for key in keys:
        rule = _lookup_unique_rule(rule_lookup, key)
        if rule:
            return rule

    if "+" in stem:
        return stem.split("+", 1)[0]

    return stem


def _collect_run_benchmark_records(run_dir, benchmark_dir):
    """Collect benchmark-backed records for one run.

    Prefers benchmark references from `snakemake.log` (stable job linkage).
    Falls back to scanning benchmark TSV files when needed.
    """
    log_path = os.path.join(run_dir, "snakemake.log")
    rule_lookup = build_benchmark_rule_lookup(log_path)
    jobs_to_files = {}
    records = []

    if os.path.exists(log_path):
        _, jobs_to_files = parse_log(read_log(log_path))
        for jobid, files in jobs_to_files.items():
            benchmark = files.get("benchmark")
            benchmark_path = resolve_benchmark_file_path(benchmark_dir, benchmark)
            if not benchmark_path:
                continue

            try:
                runtime, input_sizes, rule_name = read_benchmark_record(benchmark_path)
            except Exception as exc:
                logger.warning(
                    f"Skipping unreadable benchmark file '{benchmark_path}': {exc}"
                )
                continue

            rule = files.get("rule") or rule_name
            if not rule:
                rule = infer_rule_for_benchmark(
                    benchmark_path,
                    benchmark_dir,
                    rule_name=rule_name,
                    rule_lookup=rule_lookup,
                )

            records.append(
                {
                    "jobid": jobid,
                    "rule": rule,
                    "runtime_s": runtime,
                    "input_sizes": input_sizes,
                }
            )

    if records:
        return records, jobs_to_files

    # Fallback path for runs without usable log benchmark references.
    for benchmark_path in iter_benchmark_tsv_paths(benchmark_dir):
        try:
            runtime, input_sizes, rule_name = read_benchmark_record(benchmark_path)
        except Exception as exc:
            logger.warning(
                f"Skipping unreadable benchmark file '{benchmark_path}': {exc}"
            )
            continue

        rule = infer_rule_for_benchmark(
            benchmark_path,
            benchmark_dir,
            rule_name=rule_name,
            rule_lookup=rule_lookup,
        )
        records.append(
            {
                "jobid": None,
                "rule": rule,
                "runtime_s": runtime,
                "input_sizes": input_sizes,
            }
        )

    return records, jobs_to_files


def _compute_ancestor_input_size(record, dag, initial_input_files, input_file_sizes):
    """Compute ancestor-based input size for one job record.

    Returns tuple: (input_size_mb | None, missing_file_count)
    """
    jobid = record.get("jobid")
    if jobid is None or dag is None or jobid not in dag:
        return None, 0

    ancestor_jobs = nx.ancestors(dag, jobid)
    ancestor_jobs.add(jobid)

    ancestor_input_files = set()
    for ancestor_jobid in ancestor_jobs:
        ancestor_input_files.update(dag.nodes[ancestor_jobid].get("input", set()))

    initial_ancestor_input_files = ancestor_input_files & initial_input_files

    total_initial_input_size_ancestors = 0.0
    missing_count = 0
    for file_path in initial_ancestor_input_files:
        size = input_file_sizes.get(file_path)
        if size is None:
            missing_count += 1
            continue
        total_initial_input_size_ancestors += size

    return total_initial_input_size_ancestors, missing_count


def collect_stained_glass_input_size_rows(
    training_dir, single_run=False, modes=("total",)
):
    """Collect per-job runtime and input-size rows using sou.snakemake_benchmarks."""
    rows = []
    omitted = defaultdict(lambda: defaultdict(int))
    modes = tuple(dict.fromkeys(modes)) if modes else ("total",)

    run_dirs = [Path(p).resolve() for p in resolve_training_runs(training_dir)]
    if single_run:
        run_dirs = run_dirs[:1]
    if not run_dirs:
        return pd.DataFrame(rows), omitted

    selected_runs = set(run_dirs)
    runs = []
    run_parents = sorted({run_dir.parent for run_dir in run_dirs})
    for parent in run_parents:
        parent_runs = collect_benchmark_files_of_all_runs(parent)
        runs.extend(
            run for run in parent_runs if run.run_path.resolve() in selected_runs
        )

    if not runs:
        return pd.DataFrame(rows), omitted

    collect_benchmarks_per_rule(runs)
    if "ancestor" in modes:
        compute_ancestor_input_sizes(runs)

    for run in runs:
        run_name = run.run_name
        for rule, datapoints in run.rules.items():
            for dp in datapoints:
                runtime_s = dp.runtime
                if runtime_s is None:
                    for mode in modes:
                        omitted[mode][rule] += 1
                    continue

                for mode in modes:
                    if mode == "total":
                        input_size_mb = dp.total_input_size
                    elif mode == "ancestor":
                        input_size_mb = dp.total_initial_input_size_ancestors
                    else:
                        omitted[mode][rule] += 1
                        continue

                    if input_size_mb is None:
                        omitted[mode][rule] += 1
                        continue

                    rows.append(
                        {
                            "mode": mode,
                            "rule": rule,
                            "input_size_mb": input_size_mb,
                            "runtime_s": runtime_s,
                            "run": run_name,
                        }
                    )

    return pd.DataFrame(rows), omitted


def resolve_training_runs(path):
    path = os.path.abspath(path)
    if os.path.isfile(path):
        return [os.path.dirname(path)]
    if os.path.isdir(os.path.join(path, "benchmarks")):
        return [path]
    return sorted(
        os.path.join(path, name)
        for name in os.listdir(path)
        if os.path.isdir(os.path.join(path, name, "benchmarks"))
    )


def source_label_from_path(path):
    """Create a stable label for output filenames from an input path."""
    abs_path = os.path.abspath(path)
    if os.path.isfile(abs_path):
        abs_path = os.path.dirname(abs_path)

    label = os.path.basename(abs_path.rstrip(os.sep)) or "data"
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")
    return label or "data"


def input_size_descriptor(mode="total"):
    labels = {
        "total": "total input size",
        "primary": "primary input size",
        "ancestor": "ancestor input size",
    }
    return labels.get(mode, f"{mode} input size")


def input_size_axis_label(mode="total"):
    return f"{input_size_descriptor(mode).capitalize()} [MB]"


def calculate_correlations(rule_df, methods):
    if (
        len(rule_df) <= 1
        or rule_df["input_size_mb"].nunique() <= 1
        or rule_df["runtime_s"].nunique() <= 1
    ):
        return {method: None for method in methods}

    correlations = {}
    for method in methods:
        if method == "pearson":
            corr, _ = pearsonr(rule_df["input_size_mb"], rule_df["runtime_s"])
        elif method == "spearman":
            corr, _ = spearmanr(rule_df["input_size_mb"], rule_df["runtime_s"])
        else:
            raise ValueError(f"Unknown correlation method: {method}")
        correlations[method] = corr
    return correlations


def format_correlation_title(correlations, threshold=None):
    labels = []
    for method, corr in correlations.items():
        short = "p" if method == "pearson" else "s"
        if corr is None:
            labels.append(f"{short}=n/a")
            continue

        if threshold is None:
            labels.append(f"{short}={corr:.3f}")
        else:
            mark = "✓" if corr >= threshold else "✗"
            labels.append(f"{short}={corr:.3f} {mark}")
    return ", ".join(labels)


def print_correlation_summary(summary, threshold):
    print(f"\nCorrelation summary, threshold >= {threshold:.2f}")
    for method, values in summary.items():
        positive = sum(
            corr is not None and corr >= threshold for corr in values.values()
        )
        total = len(values)
        print(f"{method}: {positive}/{total} rules pass threshold")


def plot_direct_input_correlation(
    path,
    workflow="stained-glass",
    single_run=False,
    input_mode=SOU_INPUT_MODE,
    corr_methods=("pearson",),
    corr_threshold=0.8,
):
    run_dirs = resolve_training_runs(path)
    if single_run:
        run_dirs = run_dirs[:1]
    if not run_dirs:
        raise ValueError(f"No training runs found at {path}")

    df, omitted = collect_stained_glass_input_size_rows(
        path,
        single_run=single_run,
        modes=(input_mode,),
    )
    if df.empty:
        raise ValueError(
            "No benchmark rows found with required fields (s, input_size_mb). "
            f"Looked under training runs at: {path}"
        )

    mode_df = df[df["mode"] == input_mode].copy()
    mode_df = mode_df.dropna(subset=["rule", "input_size_mb", "runtime_s"])
    if mode_df.empty:
        raise ValueError(
            "No usable benchmark rows after parsing rule/runtime/input size values. "
            f"Looked under training runs at: {path}"
        )

    rules = sorted(mode_df["rule"].unique())
    cols = 3
    plot_rows = (len(rules) + cols - 1) // cols
    fig, axes = plt.subplots(
        plot_rows, cols, figsize=(cols * 4, plot_rows * 3), squeeze=False
    )

    summary = {method: {} for method in corr_methods}
    for ax, rule in zip(axes.flat, rules):
        rule_df = mode_df[mode_df["rule"] == rule]
        sns.scatterplot(data=rule_df, x="input_size_mb", y="runtime_s", ax=ax, s=35)
        correlations = calculate_correlations(rule_df, corr_methods)
        for method, corr in correlations.items():
            summary[method][rule] = corr
        corr_text = format_correlation_title(correlations, threshold=corr_threshold)
        omitted_count = omitted[input_mode].get(rule, 0)
        ax.set_title(f"{rule}\n{corr_text}, n={len(rule_df)}, omitted={omitted_count}")
        ax.set_xlabel(input_size_axis_label(input_mode))
        ax.set_ylabel("Runtime [s]")
        ax.grid(True, color="lightgray", linestyle="--", linewidth=0.5)

    for ax in axes.flat[len(rules) :]:
        ax.axis("off")

    suffix = (
        f"single_run_direct_{input_mode}"
        if single_run
        else f"all_runs_direct_{input_mode}"
    )
    source_label = source_label_from_path(path)
    title_scope = (
        os.path.basename(run_dirs[0])
        if single_run
        else f"{len(run_dirs)} training runs"
    )
    fig.suptitle(
        f"{workflow}: {input_size_descriptor(input_mode)} vs runtime\n{title_scope}",
        y=1.02,
    )
    fig.tight_layout()
    plt.savefig(
        os.path.join(
            SCRIPT_DIR,
            f"input_size_correlation_{workflow}_{source_label}_{suffix}.png",
        ),
        dpi=300,
        bbox_inches="tight",
    )
    plt.close(fig)
    print_correlation_summary(summary, corr_threshold)


def plot_single_training_run_direct_input_correlation(path, workflow="stained-glass"):
    plot_direct_input_correlation(path, workflow, single_run=True)


def plot_input_size_correlations(
    training_dir,
    workflow="stained-glass",
    modes=("total",),
    single_run=False,
    corr_methods=("pearson",),
    corr_threshold=0.8,
):
    run_count = len(resolve_training_runs(training_dir))
    scope = "single_run" if run_count == 1 else "all_runs"
    source_label = source_label_from_path(training_dir)
    df, omitted = collect_stained_glass_input_size_rows(
        training_dir,
        single_run=single_run,
        modes=modes,
    )

    for mode in modes:
        mode_df = df[df["mode"] == mode].copy()
        if mode_df.empty:
            continue

        rules = sorted(mode_df["rule"].unique())
        cols = 3
        rows = (len(rules) + cols - 1) // cols
        fig, axes = plt.subplots(
            rows, cols, figsize=(cols * 4, rows * 3), squeeze=False
        )

        summary = {method: {} for method in corr_methods}
        for ax, rule in zip(axes.flat, rules):
            rule_df = mode_df[mode_df["rule"] == rule]
            sns.scatterplot(data=rule_df, x="input_size_mb", y="runtime_s", ax=ax, s=25)
            correlations = calculate_correlations(rule_df, corr_methods)
            for method, corr in correlations.items():
                summary[method][rule] = corr
            corr_text = format_correlation_title(correlations, threshold=corr_threshold)
            omitted_count = omitted[mode].get(rule, 0)
            ax.set_title(
                f"{rule}\n{corr_text}, n={len(rule_df)}, omitted={omitted_count}"
            )
            ax.set_xlabel(input_size_axis_label(mode))
            ax.set_ylabel("Runtime [s]")
            ax.grid(True, color="lightgray", linestyle="--", linewidth=0.5)

        for ax in axes.flat[len(rules) :]:
            ax.axis("off")

        fig.suptitle(
            f"{workflow}: runtime correlation with {input_size_descriptor(mode)} ({run_count} run{'s' if run_count != 1 else ''})",
            y=1.01,
        )
        fig.tight_layout()
        plt.savefig(
            os.path.join(
                SCRIPT_DIR,
                f"input_size_correlation_{workflow}_{source_label}_{scope}_{mode}.png",
            ),
            dpi=300,
            bbox_inches="tight",
        )
        plt.close(fig)
        print(f"\nInput mode: {mode}")
        print_correlation_summary(summary, corr_threshold)


def _get_mode_df(training_dir, mode):
    df, _ = collect_stained_glass_input_size_rows(training_dir, modes=(mode,))
    mode_df = df[df["mode"] == mode].copy()
    if mode_df.empty:
        raise ValueError(f"No rows found for mode '{mode}' in {training_dir}")
    return mode_df


def _select_rule(mode_df, rule=None):
    if rule:
        rule_df = mode_df[mode_df["rule"] == rule].copy()
        if rule_df.empty:
            raise ValueError(f"Rule '{rule}' not found")
        return rule, rule_df
    selected = mode_df["rule"].value_counts().idxmax()
    return selected, mode_df[mode_df["rule"] == selected].copy()


def _fit_bayesian_model(rule_df, **kwargs):
    x = rule_df["input_size_mb"].to_numpy().reshape(-1, 1)
    y = rule_df["runtime_s"].to_numpy()
    model = BayesianRidge(**kwargs)
    model.fit(x, y)
    return model, x, y


def plot_prediction_with_uncertainty_band(
    training_dir, workflow, mode="total", rule=None
):
    mode_df = _get_mode_df(training_dir, mode)
    rule_name, rule_df = _select_rule(mode_df, rule)
    out = os.path.join(
        SCRIPT_DIR,
        f"bayesian_uncertainty_{workflow}_{mode}_{rule_name}.png",
    )
    if os.path.exists(out):
        print(f"skip existing {out}")
        return

    if len(rule_df) < 3 or rule_df["input_size_mb"].nunique() < 2:
        raise ValueError(f"Rule '{rule_name}' has insufficient data for regression")

    model, _, _ = _fit_bayesian_model(rule_df)
    x_min = rule_df["input_size_mb"].min()
    x_max = rule_df["input_size_mb"].max()
    x_grid = np.linspace(x_min, x_max, 200)
    mean, std = model.predict(x_grid.reshape(-1, 1), return_std=True)

    plt.figure(figsize=(7, 4))
    plt.scatter(rule_df["input_size_mb"], rule_df["runtime_s"], s=30, label="Actual")
    plt.plot(x_grid, mean, color="#1f77b4", label="Predicted mean")
    plt.fill_between(
        x_grid,
        mean - 1.96 * std,
        mean + 1.96 * std,
        color="#1f77b4",
        alpha=0.2,
        label="95% interval",
    )
    plt.xlabel(input_size_axis_label(mode))
    plt.ylabel("Runtime [s]")
    plt.title(f"{workflow} | {rule_name} | Bayesian uncertainty ({mode})")
    plt.legend()
    plt.grid(True, color="lightgray", linestyle="--", linewidth=0.5)
    plt.tight_layout()
    plt.savefig(out, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"wrote {out}")


def plot_actual_vs_predicted_runtime(training_dir, workflow, mode="total", rule=None):
    mode_df = _get_mode_df(training_dir, mode)
    rule_name, rule_df = _select_rule(mode_df, rule)
    out = os.path.join(
        SCRIPT_DIR,
        f"prediction_error_{workflow}_{mode}_{rule_name}.png",
    )
    if os.path.exists(out):
        print(f"skip existing {out}")
        return

    if len(rule_df) < 3 or rule_df["input_size_mb"].nunique() < 2:
        raise ValueError(f"Rule '{rule_name}' has insufficient data for regression")

    model, x, y = _fit_bayesian_model(rule_df)
    y_pred = model.predict(x)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    PredictionErrorDisplay.from_predictions(
        y,
        y_pred,
        kind="actual_vs_predicted",
        ax=axes[0],
        scatter_kwargs={"s": 30},
    )
    axes[0].set_title("Actual vs predicted")

    PredictionErrorDisplay.from_predictions(
        y,
        y_pred,
        kind="residual_vs_predicted",
        ax=axes[1],
        scatter_kwargs={"s": 30},
    )
    axes[1].set_title("Residuals vs predicted")

    fig.suptitle(
        f"{workflow} | {rule_name} | Bayesian prediction error ({mode})", y=1.02
    )
    fig.tight_layout()
    plt.savefig(out, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {out}")


def _collect_bayesian_coefficients(training_dir, mode="total"):
    mode_df = _get_mode_df(training_dir, mode)
    rows = []
    for rule_name, rule_df in mode_df.groupby("rule"):
        if len(rule_df) < 3 or rule_df["input_size_mb"].nunique() < 2:
            continue
        model, _, _ = _fit_bayesian_model(rule_df)
        rows.append(
            {
                "rule": rule_name,
                "coefficient": float(np.ravel(model.coef_)[0]),
            }
        )
    return pd.DataFrame(rows)


def plot_coefficient_comparison(training_dir, workflow, mode="total"):
    out = os.path.join(SCRIPT_DIR, f"bayesian_coefficients_{workflow}_{mode}.png")
    if os.path.exists(out):
        print(f"skip existing {out}")
        return

    coef_df = _collect_bayesian_coefficients(training_dir, mode)
    if coef_df.empty:
        raise ValueError("No Bayesian coefficients available for plotting")

    plt.figure(figsize=(10, 5))
    sns.barplot(data=coef_df, x="rule", y="coefficient", color="#1f77b4")
    plt.xticks(rotation=60, ha="right")
    plt.xlabel("Rule")
    plt.ylabel("Bayesian coefficient")
    plt.title(f"{workflow} | Bayesian coefficients ({mode})")
    plt.tight_layout()
    plt.savefig(out, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"wrote {out}")


def plot_coefficient_histogram(training_dir, workflow, mode="total"):
    out = os.path.join(
        SCRIPT_DIR,
        f"bayesian_coefficient_histogram_{workflow}_{mode}.png",
    )
    if os.path.exists(out):
        print(f"skip existing {out}")
        return

    coef_df = _collect_bayesian_coefficients(training_dir, mode)
    if coef_df.empty:
        raise ValueError("No Bayesian coefficients available for histogram")

    plt.figure(figsize=(7, 4))
    sns.histplot(data=coef_df, x="coefficient", bins=20, color="#1f77b4")
    plt.xlabel("Bayesian coefficient")
    plt.ylabel("Count")
    plt.title(f"{workflow} | Bayesian coefficient histogram ({mode})")
    plt.tight_layout()
    plt.savefig(out, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"wrote {out}")


def plot_posterior_with_two_gaussian_priors(
    training_dir,
    workflow,
    mode="total",
    rule=None,
    all_rules=False,
):
    mode_df = _get_mode_df(training_dir, mode)
    grouped = list(mode_df.groupby("rule"))

    if all_rules:
        selected = grouped
    else:
        rule_name, rule_df = _select_rule(mode_df, rule)
        selected = [(rule_name, rule_df)]

    # Prior A: weakly-informative (wide Gaussian prior over weights)
    weak_prior = {
        "alpha_1": 1e-6,
        "alpha_2": 1e-6,
        "lambda_1": 1e-6,
        "lambda_2": 1e-6,
    }
    # Prior B: stronger shrinkage toward zero (narrower effective prior)
    strong_prior = {
        "alpha_1": 1e-2,
        "alpha_2": 1e-2,
        "lambda_1": 1.0,
        "lambda_2": 1.0,
    }

    for rule_name, rule_df in selected:
        if len(rule_df) < 3 or rule_df["input_size_mb"].nunique() < 2:
            continue

        out = os.path.join(
            SCRIPT_DIR,
            f"bayesian_two_priors_{workflow}_{mode}_{rule_name}.png",
        )
        if os.path.exists(out):
            print(f"skip existing {out}")
            continue

        x_min = rule_df["input_size_mb"].min()
        x_max = rule_df["input_size_mb"].max()
        x_grid = np.linspace(x_min, x_max, 200)

        weak_model, _, _ = _fit_bayesian_model(rule_df, **weak_prior)
        strong_model, _, _ = _fit_bayesian_model(rule_df, **strong_prior)

        weak_mean, weak_std = weak_model.predict(x_grid.reshape(-1, 1), return_std=True)
        strong_mean, strong_std = strong_model.predict(
            x_grid.reshape(-1, 1), return_std=True
        )

        plt.figure(figsize=(8, 4.5))
        plt.scatter(
            rule_df["input_size_mb"],
            rule_df["runtime_s"],
            s=30,
            c="black",
            alpha=0.7,
            label="Actual runtime",
        )

        plt.plot(
            x_grid, weak_mean, color="#1f77b4", label="Posterior mean (weak prior)"
        )
        plt.fill_between(
            x_grid,
            weak_mean - 1.96 * weak_std,
            weak_mean + 1.96 * weak_std,
            color="#1f77b4",
            alpha=0.18,
            label="95% interval (weak prior)",
        )

        plt.plot(
            x_grid,
            strong_mean,
            color="#ff7f0e",
            label="Posterior mean (strong prior)",
        )
        plt.fill_between(
            x_grid,
            strong_mean - 1.96 * strong_std,
            strong_mean + 1.96 * strong_std,
            color="#ff7f0e",
            alpha=0.15,
            label="95% interval (strong prior)",
        )

        plt.xlabel(input_size_axis_label(mode))
        plt.ylabel("Runtime [s]")
        plt.title(
            f"{workflow} | {rule_name} | Bayesian posterior with two priors ({mode})"
        )
        plt.grid(True, color="lightgray", linestyle="--", linewidth=0.5)
        plt.legend(fontsize=8)
        plt.tight_layout()
        plt.savefig(out, dpi=300, bbox_inches="tight")
        plt.close()
        print(f"wrote {out}")


def export_bayesian_percentile_predictions(training_dir, workflow, mode="total"):
    out = os.path.join(
        SCRIPT_DIR,
        f"bayesian_percentiles_{workflow}_{mode}.json",
    )
    if os.path.exists(out):
        print(f"skip existing {out}")
        return

    mode_df = _get_mode_df(training_dir, mode)
    records = []

    for rule_name, rule_df in mode_df.groupby("rule"):
        if len(rule_df) < 3 or rule_df["input_size_mb"].nunique() < 2:
            continue

        model, x, _ = _fit_bayesian_model(rule_df)
        y_mean, y_std = model.predict(x, return_std=True)
        p50 = y_mean
        p80 = y_mean + norm.ppf(0.80) * y_std
        p90 = y_mean + norm.ppf(0.90) * y_std
        p95 = y_mean + norm.ppf(0.95) * y_std

        for i in range(len(rule_df)):
            records.append(
                {
                    "rule": rule_name,
                    "pred_mean": float(y_mean[i]),
                    "pred_std": float(y_std[i]),
                    "pred_p50": float(p50[i]),
                    "pred_p80": float(p80[i]),
                    "pred_p90": float(p90[i]),
                    "pred_p95": float(p95[i]),
                    "actual_runtime": float(rule_df["runtime_s"].iloc[i]),
                    "input_size_mb": float(rule_df["input_size_mb"].iloc[i]),
                    "model": "bayesian_ridge",
                    "model_coef": np.ravel(model.coef_).tolist(),
                    "model_sigma": np.asarray(model.sigma_).tolist(),
                    "model_alpha": float(model.alpha_),
                    "model_lambda": float(model.lambda_),
                }
            )

    with open(out, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
    print(f"wrote {out}")


def plot_bayesian_percentile_calibration(json_path, workflow="stained-glass"):
    out = os.path.join(
        SCRIPT_DIR,
        f"bayesian_percentile_calibration_{workflow}.png",
    )
    if os.path.exists(out):
        print(f"skip existing {out}")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        records = json.load(f)
    df = pd.DataFrame(records)
    if df.empty:
        raise ValueError("No rows found in percentile JSON")

    # Calibration points per rule and percentile.
    rows = []
    for rule, rule_df in df.groupby("rule"):
        for q in [50, 80, 90, 95]:
            col = f"pred_p{q}"
            if col not in rule_df:
                continue
            pred_q = rule_df[col].quantile(q / 100)
            actual_q = rule_df["actual_runtime"].quantile(q / 100)
            coverage = (rule_df["actual_runtime"] <= rule_df[col]).mean() * 100
            rows.append(
                {
                    "rule": rule,
                    "percentile": q,
                    "predicted_quantile": pred_q,
                    "actual_quantile": actual_q,
                    "empirical_coverage_percent": coverage,
                }
            )
    cdf = pd.DataFrame(rows)
    if cdf.empty:
        raise ValueError("Could not build calibration data")

    g = sns.FacetGrid(cdf, col="rule", col_wrap=3, sharex=False, sharey=False, height=3)
    g.map_dataframe(
        sns.lineplot,
        x="predicted_quantile",
        y="actual_quantile",
        marker="o",
    )
    for ax, (rule, rule_df) in zip(g.axes.flat, cdf.groupby("rule")):
        xmin, xmax = ax.get_xlim()
        ymin, ymax = ax.get_ylim()
        lo = min(xmin, ymin)
        hi = max(xmax, ymax)
        ax.plot([lo, hi], [lo, hi], linestyle="--", color="black", linewidth=1)
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)

        txt = ", ".join(
            f"p{int(r.percentile)}:{r.empirical_coverage_percent:.0f}%"
            for _, r in rule_df.sort_values("percentile").iterrows()
        )
        ax.text(
            0.03,
            0.97,
            txt,
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=8,
            bbox={"facecolor": "white", "alpha": 0.6, "edgecolor": "none"},
        )

    g.set_axis_labels("Predicted percentile runtime [s]", "Actual runtime quantile [s]")
    g.fig.suptitle(f"{workflow} | Bayesian percentile calibration", y=1.02)
    g.tight_layout()
    g.savefig(out, dpi=300, bbox_inches="tight")
    plt.close(g.fig)
    print(f"wrote {out}")


def plot_bayesian_sharpness_vs_error(json_path, workflow="stained-glass"):
    out = os.path.join(
        SCRIPT_DIR,
        f"bayesian_sharpness_vs_error_{workflow}.png",
    )
    if os.path.exists(out):
        print(f"skip existing {out}")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        records = json.load(f)
    df = pd.DataFrame(records)
    if df.empty:
        raise ValueError("No rows found in percentile JSON")

    df = df.copy()
    df["abs_error"] = (df["actual_runtime"] - df["pred_mean"]).abs()

    plt.figure(figsize=(7, 5))
    sns.scatterplot(
        data=df,
        x="pred_std",
        y="abs_error",
        hue="rule",
        s=28,
        alpha=0.8,
    )
    plt.xlabel("Predicted std (uncertainty)")
    plt.ylabel("Absolute error |actual - pred_mean| [s]")
    plt.title(f"{workflow} | Bayesian sharpness vs error")
    plt.grid(True, color="lightgray", linestyle="--", linewidth=0.5)
    plt.tight_layout()
    plt.savefig(out, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"wrote {out}")


def generate_bayesian_percentile_visuals(json_path, workflow="stained-glass"):
    plot_bayesian_percentile_calibration(json_path, workflow=workflow)
    plot_bayesian_sharpness_vs_error(json_path, workflow=workflow)


def generate_bayesian_plots(training_dir, workflow, mode="total", rule=None):
    plot_prediction_with_uncertainty_band(training_dir, workflow, mode=mode, rule=rule)
    plot_actual_vs_predicted_runtime(training_dir, workflow, mode=mode, rule=rule)
    plot_coefficient_comparison(training_dir, workflow, mode=mode)
    plot_coefficient_histogram(training_dir, workflow, mode=mode)


def read_files(log_dir, prediction_path, workflow, median_rules):
    # Recursively find all snakemake.log files in log_dir
    log_file_paths = []
    for root, dirs, files in os.walk(log_dir, followlinks=True):
        if "snakemake.log" in files:
            log_file_paths.append(os.path.join(root, "snakemake.log"))

    predictions = read_latest_predictions(prediction_path)

    job_to_wall_time_dicts = []
    jobs_to_files = None
    for log_path in log_file_paths:
        log_text = read_log(log_path)
        job_to_wall_time, jobs_to_files_current = parse_log(log_text)
        job_to_wall_time_dicts.append(job_to_wall_time)
        if jobs_to_files is None:
            jobs_to_files = jobs_to_files_current

    # Calculate differences_rel for each job_to_wall_time dict
    differences_rel_list = []
    for job_to_wall_time in job_to_wall_time_dicts:
        _, differences_rel = calculate_differences(job_to_wall_time, predictions)
        differences_rel_list.append(differences_rel)

    # Combine all differences_rel into rule_to_differences_rel
    rule_to_differences_rel = defaultdict(list)
    for differences_rel in differences_rel_list:
        for jobid, diff in differences_rel.items():
            if diff is not None:
                rule = jobs_to_files[jobid]["rule"]
                rule_to_differences_rel[rule].append(diff)

    plot_differences_relative(
        rule_to_differences_rel, median_rules=median_rules, workflow=workflow
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input-data",
        help="Path to a single run snakemake.log/run directory or to a training-runs directory",
    )
    parser.add_argument(
        "--bayesian-plots",
        help="Path to training runs directory for Bayesian plot set (uncertainty, prediction error, coefficients)",
    )

    parser.add_argument(
        "--bayesian-rule",
        help="optional rule name for uncertainty/prediction-error plots; defaults to rule with most samples",
    )
    parser.add_argument(
        "--bayesian-two-priors",
        help="Path to training runs directory for posterior plot with two Gaussian priors",
    )
    parser.add_argument(
        "--bayesian-two-priors-all-rules",
        action="store_true",
        help="with --bayesian-two-priors, generate one plot per rule",
    )
    parser.add_argument(
        "--bayesian-export-percentiles",
        help="Path to training runs directory for exporting Bayesian mean/std and p50/p80/p90/p95 per task",
    )
    parser.add_argument(
        "--bayesian-percentile-visuals",
        help="Path to bayesian_percentiles_*.json for calibration and sharpness/error visuals",
    )

    parser.add_argument(
        "--corr-method",
        choices=["pearson", "spearman", "both"],
        default="pearson",
        help="correlation method to annotate and summarize",
    )
    parser.add_argument(
        "--corr-threshold",
        type=float,
        default=0.8,
        help="threshold used for the final positive-correlation summary",
    )
    parser.add_argument("--workflow", default="stained-glass")
    args = parser.parse_args()

    if args.bayesian_two_priors:
        plot_posterior_with_two_gaussian_priors(
            args.bayesian_two_priors,
            args.workflow,
            mode=SOU_INPUT_MODE,
            rule=args.bayesian_rule,
            all_rules=args.bayesian_two_priors_all_rules,
        )
        return

    if args.bayesian_export_percentiles:
        export_bayesian_percentile_predictions(
            args.bayesian_export_percentiles,
            args.workflow,
            mode=SOU_INPUT_MODE,
        )
        return

    if args.bayesian_percentile_visuals:
        generate_bayesian_percentile_visuals(
            args.bayesian_percentile_visuals,
            workflow=args.workflow,
        )
        return

    if args.bayesian_plots:
        generate_bayesian_plots(
            args.bayesian_plots,
            args.workflow,
            mode=SOU_INPUT_MODE,
            rule=args.bayesian_rule,
        )
        return

    if args.input_data:
        single_run = os.path.isfile(args.input_data) or os.path.isdir(
            os.path.join(args.input_data, "benchmarks")
        )
        corr_methods = (
            ("pearson", "spearman")
            if args.corr_method == "both"
            else (args.corr_method,)
        )
        plot_direct_input_correlation(
            args.input_data,
            args.workflow,
            single_run=single_run,
            input_mode=SOU_INPUT_MODE,
            corr_methods=corr_methods,
            corr_threshold=args.corr_threshold,
        )
        return

    rna_dir = "../../experimental-data/rna-seq-star-deseq2/experiments"
    rna_median_rules = [
        "rseqc_innerdis",
        "rseqc_gtf2bed",
        "deseq2_init",
        "deseq2",
        "gene_2_symbol",
        "multiqc",
        "count_matrix",
    ]

    read_files(
        rna_dir,
        "latest_predictions_runtime_rna.json",
        "rna-seq-star-deseq2",
        rna_median_rules,
    )

    stained_dir = "../../experimental-data/stained-glass/experiments"
    stained_median_rules = [
        "aln",
        "split_windows",
        "merge_list",
        "identity",
        "make_windows",
    ]

    read_files(
        stained_dir,
        "latest_predictions_runtime_stained.json",
        "stained-glass",
        stained_median_rules,
    )
    stained_training_dir = "../../experimental-data/stained-glass/training"
    plot_single_training_run_direct_input_correlation(
        stained_training_dir,
        "stained-glass",
    )
    plot_input_size_correlations(
        stained_training_dir,
        "stained-glass",
    )

    dna_dir = "../../experimental-data/dna-seq-varlociraptor/experiments"
    dna_median_rules = [
        "merge_expanded_group_regions",
        "varlociraptor_preprocess",
        "annotate_variants",
        "filter_candidates_by_annotation",
        "annotated_index",
        "bcftools_concat",
        "annotate_candidate_variants",
        "sort_calls",
        "annotate_vcfs",
        "control_fdr",
        "map_reads_bwa",
        "filter_by_annotation",
        "gather_calls",
        "tsv_to_excel",
        "varlociraptor",
        "varlociraptor_postprocess",
        "varlociraptor_merge",
        "varlociraptor_filter",
        "varlociraptor_annotate",
        "varlociraptor_sort",
        "varlociraptor_alignment_properties",
        "render_scenario",
        "bedtools_merge",
        "freebayes",
        "scatter_candidates",
        "delly",
        "sort_alignments",
        "vembrane_table",
        "coverage_table",
        "group_bcf_to_vcf",
        "merge_covered_group_regions",
        "build_sample_regions",
        "datavzrd_coverage",
        "multiqc",
        "convert_phred_scores",
        "merge_calls",
        "fix_delly_calls",
        "recalibrate_base_qualities",
        "samtools_idxstats",
        "filter_group_regions",
    ]

    read_files(
        dna_dir,
        "latest_predictions_runtime_dna.json",
        "dna-seq-varlociraptor",
        dna_median_rules,
    )


if __name__ == "__main__":
    main()
