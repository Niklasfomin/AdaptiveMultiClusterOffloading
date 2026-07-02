import json
import re
import os
import logging
from collections import defaultdict
from datetime import datetime
import seaborn as sns
import matplotlib.pyplot as plt
import pandas as pd

logger = logging.getLogger()


def remove_storage_prefix(path: str) -> str:
    snakemake_pattern = r'^\.snakemake/storage/[^/]+/'
    protocol_pattern = r'^\w+://'
    if re.match(snakemake_pattern, path):
        return re.sub(snakemake_pattern, '', path)
    elif re.match(protocol_pattern, path):
        return re.sub(protocol_pattern, '', path)
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

    file_pattern = r'[^\s()]+(?=\s*\([^()]*\)\s*(?:,|$))'

    for line in log_text.splitlines():
        line = line.strip()

        if timestamp_match := re.match(r'\[(\w{3} \w{3}  ?\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\]', line):
            current_timestamp = datetime.strptime(timestamp_match.group(1), "%a %b %d %H:%M:%S %Y")

        elif line.startswith("rule ") or line.startswith("localrule "):
            rule = re.search(r'rule (\S+):', line).group(1)
            current_job_files = {'input': [], 'output': [], 'log': [], 'benchmark': None, 'rule': rule}

        elif line.startswith("input:"):
            inputs = re.findall(file_pattern, line)
            inputs = [remove_storage_prefix(f.strip()) for f in inputs]
            current_job_files['input'] = inputs

        elif line.startswith("output:"):
            outputs = re.findall(file_pattern, line)
            outputs = [remove_storage_prefix(f.strip()) for f in outputs]
            current_job_files['output'] = outputs

        elif line.startswith("log:"):
            logs = re.findall(file_pattern, line)
            logs = [remove_storage_prefix(f.strip()) for f in logs]
            current_job_files['log'] = logs

        elif line.startswith("benchmark:"):
            benchmark = re.match(r'benchmark:\s+(\S+)\s+\(', line).group(1)
            benchmark = remove_storage_prefix(benchmark.strip())
            current_job_files['benchmark'] = benchmark

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
                logger.debug(f"Found job {jobid} with associated files: {current_job_files}")

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
                logger.warning(f"None value for job {jobid} in predictions or wall time")
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
                f"varlociraptor_preprocess: n={n}, mean={mean:.3f}, std={std:.3f}, median={median:.3f}, Q1={q1:.3f}, Q3={q3:.3f}, min={min_val:.3f}, max={max_val:.3f}, skewness={skew:.3f}, kurtosis={kurt:.3f}")
            if abs(skew) > 1:
                print(f"The distribution is highly {'right' if skew > 0 else 'left'}-skewed.")
            elif abs(skew) > 0.5:
                print(f"The distribution is moderately {'right' if skew > 0 else 'left'}-skewed.")
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
    ax = sns.boxplot(x="Rule", y="Value", hue="Category", data=df, palette=palette, order=plot_rules)
    # Set smaller font for dna-seq-varlociraptor
    if workflow == "dna-seq-varlociraptor":
        plt.xticks(rotation=60, ha='right', fontsize=10)
        ax.set_ylabel("Prediction Error (%)", fontsize=14)
        ax.set_xlabel("Task", fontsize=13)
        ax.legend(title="Prediction type", fontsize=13, title_fontsize=13)
        plt.title(workflow, fontsize=16)
    else:
        plt.xticks(rotation=60, ha='right')
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
    plt.grid(True, color='lightgray', linestyle='--', linewidth=0.5)
    # Set legend title for non-dna plots
    if workflow != "dna-seq-varlociraptor":
        handles, labels = ax.get_legend_handles_labels()
        ax.legend(handles, labels, title="Prediction type")
    plt.savefig(f"job_runtime_prediction_relative_{workflow}.png", dpi=300, bbox_inches="tight")
    plt.savefig(f"job_runtime_prediction_relative_{workflow}.pdf", bbox_inches="tight")
    plt.show()


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
                rule = jobs_to_files[jobid]['rule']
                rule_to_differences_rel[rule].append(diff)

    plot_differences_relative(rule_to_differences_rel, median_rules=median_rules, workflow=workflow)


if __name__ == "__main__":
    rna_dir = "../../experimental-data/rna-seq-star-deseq2/experiments"
    rna_median_rules = [
        "rseqc_innerdis", "rseqc_gtf2bed", "deseq2_init", "deseq2", "gene_2_symbol", "multiqc", "count_matrix"
    ]

    read_files(rna_dir, "latest_predictions_runtime_rna.json", "rna-seq-star-deseq2", rna_median_rules)

    stained_dir = "../../experimental-data/stained-glass/experiments"
    stained_median_rules = [
        "aln", "split_windows", "merge_list", "identity", "make_windows"
    ]

    read_files(stained_dir, "latest_predictions_runtime_stained.json", "stained-glass", stained_median_rules)

    dna_dir = "../../experimental-data/dna-seq-varlociraptor/experiments"
    dna_median_rules = [
        "merge_expanded_group_regions", "varlociraptor_preprocess", "annotate_variants",
        "filter_candidates_by_annotation", "annotated_index", "bcftools_concat", "annotate_candidate_variants",
        "sort_calls", "annotate_vcfs", "control_fdr", "map_reads_bwa", "filter_by_annotation", "gather_calls",
        "tsv_to_excel", "varlociraptor", "varlociraptor_postprocess", "varlociraptor_merge", "varlociraptor_filter",
        "varlociraptor_annotate", "varlociraptor_sort", "varlociraptor_alignment_properties", "render_scenario",
        "bedtools_merge", "freebayes", "scatter_candidates", "delly", "sort_alignments", "vembrane_table",
        "coverage_table", "group_bcf_to_vcf", "merge_covered_group_regions", "build_sample_regions",
        "datavzrd_coverage", "multiqc", "convert_phred_scores", "merge_calls", "fix_delly_calls",
        "recalibrate_base_qualities", "samtools_idxstats", "filter_group_regions",
    ]

    read_files(dna_dir, "latest_predictions_runtime_dna.json", "dna-seq-varlociraptor", dna_median_rules)
