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
import pandas as pd
import seaborn as sns
from scipy.stats import pearsonr, spearmanr

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
