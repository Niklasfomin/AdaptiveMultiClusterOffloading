import os
import re
import numpy as np
from datetime import datetime
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt

TIMESTAMP_PATTERN = re.compile(r'\[(\w{3} \w{3}  ?\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\]')

def extract_runtime_from_log(log_path):
    timestamps = []
    with open(log_path, "r") as f:
        for line in f:
            match = TIMESTAMP_PATTERN.match(line)
            if match:
                timestamps.append(datetime.strptime(match.group(1), "%a %b %d %H:%M:%S %Y"))
    if len(timestamps) >= 2:
        return (max(timestamps) - min(timestamps)).total_seconds()
    else:
        return None

def collect_runtimes(base_dir, label):
    runtimes = []
    for root, dirs, files in os.walk(base_dir):
        if "snakemake.log" in files:
            log_path = os.path.join(root, "snakemake.log")
            runtime = extract_runtime_from_log(log_path)
            if runtime is not None:
                runtimes.append({"runtime_minutes": runtime / 60, "offloading": label})
    return runtimes

def main(off_dir, noff_dir, pred_offloading=None, pred_no_offloading=None, plot_title=""):
    order = ["no_offloading", "offloading"]
    label_map = {
        "no_offloading": "Inactive",
        "offloading": "PEFO"
    }

    data = collect_runtimes(noff_dir, "no_offloading") + collect_runtimes(off_dir, "offloading")
    df = pd.DataFrame(data)
    print("[DEBUG] data:", data)
    print("[DEBUG] df.columns:", df.columns.tolist())

    # Print median and prediction for both categories
    medians = {}
    predictions = {"no_offloading": pred_no_offloading, "offloading": pred_offloading}
    for group in ["no_offloading", "offloading"]:
        group_data = df[df["offloading"] == group]["runtime_minutes"]
        if not group_data.empty:
            median_min = group_data.median()
            medians[group] = median_min
            pred_min = predictions[group] / 60 if predictions[group] is not None else None
            print(f"{label_map[group]}: Median = {median_min:.2f} min, Prediction = {pred_min:.2f} min")
        else:
            print(f"{label_map[group]}: No data available")

    # Print difference if both medians are available
    if all(g in medians for g in ["no_offloading", "offloading"]):
        diff = medians["offloading"] - medians["no_offloading"]
        percent = (diff / medians["no_offloading"] * 100) if medians["no_offloading"] != 0 else float('nan')
        print(f"Difference (PEFO - Inactive): {diff*60:.2f} seconds ({percent:.2f}%)")

    # Box plot (compact)
    sns.set(style="whitegrid")
    plt.figure(figsize=(4, 4))
    ax = sns.boxplot(data=df, x="offloading", y="runtime_minutes", order=order, width=0.4, color="#D3D3D3")
    # Increase margins to bring categories closer together
    ax.set_xlim(-0.25, 1.25)
    # Expand y-axis if needed to ensure PE text is always visible
    y_min, y_max = ax.get_ylim()
    # Find all prediction values to check if PE text would be outside
    pred_vals = []
    if pred_no_offloading is not None:
        pred_vals.append(pred_no_offloading / 60)
    if pred_offloading is not None:
        pred_vals.append(pred_offloading / 60)
    for pred_val in pred_vals:
        y_text = pred_val - 0.03 * (y_max - y_min)
        if y_text < y_min:
            # Expand lower boundary
            y_min = y_text - 0.05 * (y_max - y_min)
        if y_text > y_max:
            # Expand upper boundary
            y_max = y_text + 0.05 * (y_max - y_min)
    ax.set_ylim(y_min, y_max)

    # Customize x-axis labels
    ax.set_xticklabels([label_map[label] for label in order])
    ax.set_xlabel("Offloading", fontsize=12)
    ax.set_title(plot_title, fontsize=14)
    ax.set_ylabel("Workflow makespan (minutes)", fontsize=12)
    ax.tick_params(axis='both', which='major', labelsize=12)

    # Draw local dashed prediction lines
    pred_color = "orange"
    xticks = ax.get_xticks()
    box_width = 0.4
    line_width = box_width / 1.5

    lines = []
    labels = []

    # Make prediction line as wide as the box plot using PathPatch vertices
    # For seaborn boxplot, the first two patches are the main boxes
    # First, collect all y_text positions for PE
    pe_texts = []
    x_centers = []
    pe_labels = []
    if len(ax.patches) >= 2:
        for i in range(2):
            box = ax.patches[i]
            path = box.get_path().vertices
            x_left = min(path[:, 0])
            x_right = max(path[:, 0])
            pred_val = pred_no_offloading / 60 if i == 0 else pred_offloading / 60
            label = "Prediction" if i == 0 else None
            line = ax.plot([x_left, x_right], [pred_val, pred_val], ls="-", color=pred_color, label=label)
            group = order[i]
            group_data = df[df["offloading"] == group]["runtime_minutes"]
            if not group_data.empty:
                median_val = group_data.median()
                pe = (pred_val - median_val) / median_val * 100
                x_center = (x_left + x_right) / 2
                x_centers.append(x_center)
                pe_labels.append(f"PE={pe:.2f}%")
                # Use initial y_min/y_max for calculation
                y_min, y_max = ax.get_ylim()
                y_text = pred_val - 0.03 * (y_max - y_min)
                pe_texts.append(y_text)
            if i == 0:
                lines.append(line[0])
                labels.append("Prediction")
        # After collecting, expand y-axis if needed
        y_min, y_max = ax.get_ylim()
        # Force a minimum margin above the x-axis for the lowest PE text
        min_y_text = min(pe_texts)
        min_margin = 0.04 * (y_max - y_min)  # Minimum margin above x-axis for PE text (smaller)
        if min_y_text < y_min + min_margin:
            y_min = min_y_text - min_margin
        # Also check upper boundary (not strictly needed for PE below line, but for completeness)
        max_y_text = max(pe_texts)
        if max_y_text > y_max:
            margin = 0.05 * (y_max - y_min)
            y_max = max_y_text + margin
        ax.set_ylim(y_min, y_max)
        # Now plot the PE text
        y_min, y_max = ax.get_ylim()
        for i in range(2):
            pred_val = pred_no_offloading / 60 if i == 0 else pred_offloading / 60
            y_text = pred_val - 0.03 * (y_max - y_min)
            ax.text(x_centers[i], y_text, pe_labels[i], color=pred_color, ha="center", va="top", fontsize=10)
    else:
        # Fallback to old logic if box patches not available
        if pred_no_offloading is not None:
            x = xticks[0]
            line = ax.plot(
                [x - box_width, x + box_width],
                [pred_no_offloading / 60] * 2,
                ls="-", color=pred_color, label="Prediction"
            )
            lines.append(line[0])
            labels.append("Prediction")
        if pred_offloading is not None:
            x = xticks[1]
            line = ax.plot(
                [x - box_width, x + box_width],
                [pred_offloading / 60] * 2,
                ls="-", color=pred_color
            )
            if not labels:
                lines.append(line[0])
                labels.append("Prediction")

    if labels:
        ax.legend(lines, labels, fontsize=12)
    plt.tight_layout(pad=1.0)
    plt.savefig(f"workflow_makespan_boxplot_{plot_title}.png", dpi=600, bbox_inches='tight')  # Save in high resolution
    plt.savefig(f"workflow_makespan_boxplot_{plot_title}.pdf", bbox_inches='tight')  # Save as PDF
    plt.close()

    # Scatter plot (compact)
    plt.figure(figsize=(4, 4))
    np.random.seed(42)  # Set random seed for deterministic jitter
    ax2 = sns.stripplot(data=df, x="offloading", y="runtime_minutes", order=order, jitter=0.2, size=6, alpha=0.7, color="grey", dodge=True)
    ax2.set_xticklabels([label_map[label] for label in order])
    ax2.set_xlabel("Offloading", fontsize=12)
    ax2.set_title(plot_title, fontsize=14)
    ax2.set_ylabel("Workflow makespan (minutes)", fontsize=12)
    ax2.tick_params(axis='both', which='major', labelsize=12)
    # Expand y-axis if needed to ensure PE text is always visible
    y_min2, y_max2 = ax2.get_ylim()
    pe_texts2 = []
    pred_vals2 = []
    if pred_no_offloading is not None:
        pred_vals2.append(pred_no_offloading / 60)
    if pred_offloading is not None:
        pred_vals2.append(pred_offloading / 60)
    for pred_val in pred_vals2:
        y_text = pred_val - 0.03 * (y_max2 - y_min2)
        pe_texts2.append(y_text)
    # Enforce minimum margin above x-axis for lowest PE text
    if pe_texts2:
        min_y_text2 = min(pe_texts2)
        min_margin2 = 0.08 * (y_max2 - y_min2)
        if min_y_text2 < y_min2 + min_margin2:
            y_min2 = min_y_text2 - min_margin2
        max_y_text2 = max(pe_texts2)
        if max_y_text2 > y_max2:
            margin2 = 0.05 * (y_max2 - y_min2)
            y_max2 = max_y_text2 + margin2
        ax2.set_ylim(y_min2, y_max2)

    # Draw prediction lines and median lines on scatter plot
    pred_color = "orange"
    median_color = "black"
    xticks2 = ax2.get_xticks()
    box_width = 0.4
    lines2 = []
    labels2 = []
    # Prediction lines
    if pred_no_offloading is not None:
        x = xticks2[0]
        pred_val = pred_no_offloading / 60
        line2 = ax2.plot(
            [x - line_width, x + line_width],
            [pred_val] * 2,
            ls="-", color=pred_color, label="Prediction"
        )
        # Calculate median and PE
        group_data = df[df["offloading"] == order[0]]["runtime_minutes"]
        if not group_data.empty:
            median_val = group_data.median()
            pe = (pred_val - median_val) / median_val * 100
            x_center = x
            y_min, y_max = ax2.get_ylim()
            y_text = pred_val - 0.03 * (y_max - y_min)
            # Expand y-axis if needed
            if y_text < y_min:
                y_min = y_text - 0.05 * (y_max - y_min)
            if y_text > y_max:
                y_max = y_text + 0.05 * (y_max - y_min)
            ax2.set_ylim(y_min, y_max)
            y_min, y_max = ax2.get_ylim()
            y_text = pred_val - 0.03 * (y_max - y_min)
            ax2.text(x_center, y_text, f"PE={pe:.2f}%", color=pred_color, ha="center", va="top", fontsize=11)
        lines2.append(line2[0])
        labels2.append("Prediction")
    if pred_offloading is not None:
        x = xticks2[1]
        pred_val = pred_offloading / 60
        line2 = ax2.plot(
            [x - line_width, x + line_width],
            [pred_val] * 2,
            ls="-", color=pred_color
        )
        # Calculate median and PE
        group_data = df[df["offloading"] == order[1]]["runtime_minutes"]
        if not group_data.empty:
            median_val = group_data.median()
            pe = (pred_val - median_val) / median_val * 100
            x_center = x
            y_min, y_max = ax2.get_ylim()
            y_text = pred_val - 0.03 * (y_max - y_min)
            # Expand y-axis if needed
            if y_text < y_min:
                y_min = y_text - 0.05 * (y_max - y_min)
            if y_text > y_max:
                y_max = y_text + 0.05 * (y_max - y_min)
            ax2.set_ylim(y_min, y_max)
            y_min, y_max = ax2.get_ylim()
            y_text = pred_val - 0.03 * (y_max - y_min)
            ax2.text(x_center, y_text, f"PE={pe:.2f}%", color=pred_color, ha="center", va="top", fontsize=11)
        if not labels2:
            lines2.append(line2[0])
            labels2.append("Prediction")
    # Median lines
    for i, group in enumerate(order):
        group_data = df[df["offloading"] == group]["runtime_minutes"]
        if not group_data.empty:
            median_val = group_data.median()
            median_line = ax2.plot(
                [xticks2[i] - line_width, xticks2[i] + line_width],
                [median_val] * 2,
                ls="-", color=median_color, label="Median" if i == 0 else None
            )
            if i == 0:
                lines2.append(median_line[0])
                labels2.append("Median")
    if labels2:
        ax2.legend(lines2, labels2, fontsize=12)

    plt.tight_layout(pad=1.0)
    plt.savefig(f"workflow_makespan_scatter_{plot_title}.png", dpi=600, bbox_inches='tight')
    plt.savefig(f"workflow_makespan_scatter_{plot_title}.pdf", bbox_inches='tight')
    plt.close()

if __name__ == "__main__":
    main(
        "../../experimental-data/rna-seq-star-deseq2/experiments/offloading/pcfo",
        "../../experimental-data/rna-seq-star-deseq2/experiments/no_offloading",
        pred_no_offloading=23291.76,
        pred_offloading=13349.72,
        plot_title="rna-seq-star-deseq2"
    )
    main(
        "../../experimental-data/stained-glass/experiments/offloading/pcfo",
        "../../experimental-data/stained-glass/experiments/no_offloading",
        pred_no_offloading=4702.64,
        pred_offloading=3212.04,
        plot_title="stained-glass"
    )
    main(
        "../../experimental-data/dna-seq-varlociraptor/experiments/offloading/pcfo",
        "../../experimental-data/dna-seq-varlociraptor/experiments/no_offloading",
        pred_no_offloading=11967.69,
        pred_offloading=8088.07,
        plot_title="dna-seq-varlociraptor"
    )
