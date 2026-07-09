import argparse
import ast
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime

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


def remove_storage_prefix(path: str) -> str:
    snakemake_pattern = r"^\.snakemake/storage/[^/]+/"
    protocol_pattern = r"^\w+://"
    if re.match(snakemake_pattern, path):
        return re.sub(snakemake_pattern, "", path)
    elif re.match(protocol_pattern, path):
        return re.sub(protocol_pattern, "", path)
    else:
        logger.warning(f"Path doesn't match expected storage prefix: {path}")
        return path


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

    file_pattern = r"[^\s()]+(?=\s*\([^()]*\)\s*(?:,|$))"

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


def read_benchmark_input_sizes(path):
    df = pd.read_csv(path, sep="\t")
    runtime = float(df["s"].iloc[0])
    input_sizes = ast.literal_eval(df["input_size_mb"].iloc[0])
    input_sizes = {remove_storage_prefix(k): v for k, v in input_sizes.items()}
    return runtime, input_sizes


def collect_stained_glass_input_size_rows(training_dir):
    rows = []
    omitted = defaultdict(lambda: defaultdict(int))

    for run_dir in resolve_training_runs(training_dir):
        run_name = os.path.basename(run_dir)
        benchmark_dir = os.path.join(run_dir, "benchmarks")
        log_path = os.path.join(run_dir, "snakemake.log")
        if not os.path.isdir(benchmark_dir) or not os.path.exists(log_path):
            continue

        _, jobs_to_files = parse_log(read_log(log_path))
        initial_inputs = get_initial_input_files(jobs_to_files)
        dag = reconstruct_dag(jobs_to_files)

        job_input_sizes = {}
        for jobid, files in jobs_to_files.items():
            benchmark = files.get("benchmark")
            if not benchmark:
                continue
            benchmark_name = benchmark.split("benchmarks/")[-1].replace("/", "_")
            benchmark_path = os.path.join(benchmark_dir, benchmark_name)
            if not os.path.exists(benchmark_path):
                continue
            runtime, input_sizes = read_benchmark_input_sizes(benchmark_path)
            job_input_sizes[jobid] = input_sizes
            rule = files["rule"]

            total_size = sum(input_sizes.values())
            primary_size = sum(
                size for path, size in input_sizes.items() if path in initial_inputs
            )

            rows.append(
                {
                    "mode": "total",
                    "rule": rule,
                    "input_size_mb": total_size,
                    "runtime_s": runtime,
                }
            )
            if primary_size > 0:
                rows.append(
                    {
                        "mode": "primary",
                        "rule": rule,
                        "input_size_mb": primary_size,
                        "runtime_s": runtime,
                    }
                )
            else:
                omitted["primary"][rule] += 1

        all_known_sizes = {}
        for input_sizes in job_input_sizes.values():
            all_known_sizes.update(input_sizes)

        for jobid, input_sizes in job_input_sizes.items():
            rule = jobs_to_files[jobid]["rule"]
            benchmark = jobs_to_files[jobid].get("benchmark")
            benchmark_name = benchmark.split("benchmarks/")[-1].replace("/", "_")
            runtime, _ = read_benchmark_input_sizes(
                os.path.join(benchmark_dir, benchmark_name)
            )
            ancestor_jobs = nx.ancestors(dag, jobid)
            ancestor_jobs.add(jobid)
            ancestor_inputs = set()
            for ancestor in ancestor_jobs:
                ancestor_inputs.update(jobs_to_files[ancestor].get("input", []))
            primary_ancestor_inputs = ancestor_inputs & initial_inputs
            ancestor_size = sum(
                all_known_sizes.get(path, 0) for path in primary_ancestor_inputs
            )
            if ancestor_size > 0:
                rows.append(
                    {
                        "mode": "ancestor",
                        "rule": rule,
                        "input_size_mb": ancestor_size,
                        "runtime_s": runtime,
                    }
                )
            else:
                omitted["ancestor"][rule] += 1

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


def format_correlation_title(correlations):
    labels = []
    for method, corr in correlations.items():
        short = "p" if method == "pearson" else "s"
        labels.append(f"{short}=n/a" if corr is None else f"{short}={corr:.2f}")
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
    corr_methods=("pearson",),
    corr_threshold=0.8,
):
    run_dirs = resolve_training_runs(path)
    if single_run:
        run_dirs = run_dirs[:1]
    if not run_dirs:
        raise ValueError(f"No training runs found at {path}")

    rows = []
    for run_dir in run_dirs:
        benchmark_dir = os.path.join(run_dir, "benchmarks")
        for filename in sorted(os.listdir(benchmark_dir)):
            if "+" not in filename or not filename.endswith(".tsv"):
                continue
            rule = filename.split("+", 1)[0]
            runtime, input_sizes = read_benchmark_input_sizes(
                os.path.join(benchmark_dir, filename)
            )
            rows.append(
                {
                    "rule": rule,
                    "input_size_mb": sum(input_sizes.values()),
                    "runtime_s": runtime,
                    "run": os.path.basename(run_dir),
                }
            )

    df = pd.DataFrame(rows)
    rules = sorted(df["rule"].unique())
    cols = 3
    plot_rows = (len(rules) + cols - 1) // cols
    fig, axes = plt.subplots(
        plot_rows, cols, figsize=(cols * 4, plot_rows * 3), squeeze=False
    )

    summary = {method: {} for method in corr_methods}
    for ax, rule in zip(axes.flat, rules):
        rule_df = df[df["rule"] == rule]
        sns.scatterplot(data=rule_df, x="input_size_mb", y="runtime_s", ax=ax, s=35)
        correlations = calculate_correlations(rule_df, corr_methods)
        for method, corr in correlations.items():
            summary[method][rule] = corr
        corr_text = format_correlation_title(correlations)
        ax.set_title(f"{rule}\n{corr_text}, n={len(rule_df)}")
        ax.set_xlabel("Direct input size [MB]")
        ax.set_ylabel("Runtime [s]")
        ax.grid(True, color="lightgray", linestyle="--", linewidth=0.5)

    for ax in axes.flat[len(rules) :]:
        ax.axis("off")

    suffix = "single_run_direct_total" if single_run else "all_runs_direct_total"
    title_scope = (
        os.path.basename(run_dirs[0])
        if single_run
        else f"{len(run_dirs)} training runs"
    )
    fig.suptitle(f"{workflow}: direct input size vs runtime\n{title_scope}", y=1.02)
    fig.tight_layout()
    plt.savefig(
        os.path.join(SCRIPT_DIR, f"input_size_correlation_{workflow}_{suffix}.png"),
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
    modes=("total", "primary", "ancestor"),
    single_run=False,
    corr_methods=("pearson",),
    corr_threshold=0.8,
):
    run_count = len(resolve_training_runs(training_dir))
    scope = "single_run" if run_count == 1 else "all_runs"
    df, omitted = collect_stained_glass_input_size_rows(training_dir)
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
            corr_text = format_correlation_title(correlations)
            omitted_count = omitted[mode].get(rule, 0)
            ax.set_title(
                f"{rule}\n{corr_text}, n={len(rule_df)}, omitted={omitted_count}"
            )
            ax.set_xlabel("Input size [MB]")
            ax.set_ylabel("Runtime [s]")
            ax.grid(True, color="lightgray", linestyle="--", linewidth=0.5)

        for ax in axes.flat[len(rules) :]:
            ax.axis("off")

        fig.suptitle(
            f"{workflow}: runtime correlation with {mode} input size ({run_count} run{'s' if run_count != 1 else ''})",
            y=1.01,
        )
        fig.tight_layout()
        plt.savefig(
            os.path.join(
                SCRIPT_DIR, f"input_size_correlation_{workflow}_{scope}_{mode}.png"
            ),
            dpi=300,
            bbox_inches="tight",
        )
        plt.close(fig)
        print(f"\nInput mode: {mode}")
        print_correlation_summary(summary, corr_threshold)


def _get_mode_df(training_dir, mode):
    df, _ = collect_stained_glass_input_size_rows(training_dir)
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
    plt.xlabel("Input size [MB]")
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

        plt.xlabel("Input size [MB]")
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
        "--input-correlation",
        help="Path to a single run snakemake.log/run directory or to a training-runs directory",
    )
    parser.add_argument(
        "--bayesian-plots",
        help="Path to training runs directory for Bayesian plot set (uncertainty, prediction error, coefficients)",
    )
    parser.add_argument(
        "--bayesian-mode",
        choices=["total", "primary", "ancestor"],
        default="total",
        help="input-size mode used for Bayesian plot set",
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
        "--input-mode",
        choices=["total", "primary", "ancestor"],
        default="total",
        help="input size type to plot",
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
            mode=args.bayesian_mode,
            rule=args.bayesian_rule,
            all_rules=args.bayesian_two_priors_all_rules,
        )
        return

    if args.bayesian_export_percentiles:
        export_bayesian_percentile_predictions(
            args.bayesian_export_percentiles,
            args.workflow,
            mode=args.bayesian_mode,
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
            mode=args.bayesian_mode,
            rule=args.bayesian_rule,
        )
        return

    if args.input_correlation:
        single_run = os.path.isfile(args.input_correlation) or os.path.isdir(
            os.path.join(args.input_correlation, "benchmarks")
        )
        corr_methods = (
            ("pearson", "spearman")
            if args.corr_method == "both"
            else (args.corr_method,)
        )
        if args.input_mode == "total":
            plot_direct_input_correlation(
                args.input_correlation,
                args.workflow,
                single_run=single_run,
                corr_methods=corr_methods,
                corr_threshold=args.corr_threshold,
            )
        else:
            plot_input_size_correlations(
                args.input_correlation,
                args.workflow,
                modes=[args.input_mode],
                single_run=single_run,
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
