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

def collect_runtimes_for_deadline(base_dir, label, deadline):
    """
    Collect runtimes for a specific deadline (subdir name) in base_dir.
    """
    runtimes = []
    subdir_path = os.path.join(base_dir, deadline)
    if not os.path.isdir(subdir_path):
        return runtimes
    for root, dirs, files in os.walk(subdir_path):
        if "snakemake.log" in files:
            log_path = os.path.join(root, "snakemake.log")
            runtime = extract_runtime_from_log(log_path)
            if runtime is not None:
                runtimes.append({"runtime_minutes": runtime / 60, "offloading": label})
    return runtimes


def main(ljf_dir, sisf_dir, plot_title="", predictions=None):
    # Determine which dirs are present
    only_ljf = ljf_dir is not None and sisf_dir is None
    only_sisf = sisf_dir is not None and ljf_dir is None
    both = ljf_dir is not None and sisf_dir is not None

    # Find deadlines
    if only_ljf:
        ljf_deadlines = set([d for d in os.listdir(ljf_dir) if os.path.isdir(os.path.join(ljf_dir, d)) and d.isdigit()])
        deadlines = sorted(ljf_deadlines, key=int)
    elif only_sisf:
        sisf_deadlines = set([d for d in os.listdir(sisf_dir) if os.path.isdir(os.path.join(sisf_dir, d)) and d.isdigit()])
        deadlines = sorted(sisf_deadlines, key=int)
    elif both:
        ljf_deadlines = set([d for d in os.listdir(ljf_dir) if os.path.isdir(os.path.join(ljf_dir, d)) and d.isdigit()])
        sisf_deadlines = set([d for d in os.listdir(sisf_dir) if os.path.isdir(os.path.join(sisf_dir, d)) and d.isdigit()])
        deadlines = sorted(ljf_deadlines & sisf_deadlines, key=int)
    else:
        deadlines = []

    # Determine smallest and largest deadline for line style
    if deadlines:
        min_deadline = min(deadlines, key=int)
        max_deadline = max(deadlines, key=int)
    for deadline in deadlines:
        if only_ljf:
            data = collect_runtimes_for_deadline(ljf_dir, "ljf_sisf", deadline)
        elif only_sisf:
            data = collect_runtimes_for_deadline(sisf_dir, "ljf_sisf", deadline)
        else:
            data = collect_runtimes_for_deadline(ljf_dir, "ljf", deadline) + collect_runtimes_for_deadline(sisf_dir, "sisf", deadline)
        df = pd.DataFrame(data)
        if df.empty:
            continue

        # Get prediction values for this deadline if provided
        pred_ljf = pred_sisf = None
        pred_ljf_sisf = None
        if predictions and deadline in predictions:
            if only_ljf or only_sisf:
                # Use SISF or LJF prediction if present, else None
                pred_ljf_sisf = predictions[deadline].get("SISF") or predictions[deadline].get("LJF")
            else:
                pred_ljf = predictions[deadline].get("LJF")
                pred_sisf = predictions[deadline].get("SISF")

        # Box plot (compact)
        sns.set(style="whitegrid")
        plt.figure(figsize=(4, 4))
        if only_ljf or only_sisf:
            order = ["ljf_sisf"]
            label_map = {"ljf_sisf": "LJF/SISF"}
            box_width = 0.3
            line_width = 0.3 / 1.5
        else:
            order = ["ljf", "sisf"]
            label_map = {"ljf": "LJF", "sisf": "SISF"}
            box_width = 0.4
            line_width = 0.4 / 1.5
        ax = sns.boxplot(data=df, x="offloading", y="runtime_minutes", order=order, width=box_width, color="#D3D3D3")
        ax.set_xticklabels([label_map[label] for label in order], fontsize=12)
        ax.set_xlabel("Offloading", fontsize=12)
        # ax.set_title(f"{plot_title}", fontsize=14)  # Title removed
        ax.set_ylabel("Workflow makespan (minutes)", fontsize=12)
        ax.tick_params(axis='both', which='major', labelsize=12)
        # Center single-category box for stained-glass
        if only_ljf or only_sisf:
            ax.set_xlim(-0.5, 0.5)
            ax.set_xticks([0])
            ax.set_xticklabels([label_map[order[0]]], fontsize=12)

        # Expand y-axis if needed to ensure PE text is always visible
        y_min, y_max = ax.get_ylim()
        pred_vals = []
        if only_ljf or only_sisf:
            if pred_ljf_sisf is not None:
                pred_vals.append(pred_ljf_sisf / 60)
        else:
            if pred_ljf is not None:
                pred_vals.append(pred_ljf / 60)
            if pred_sisf is not None:
                pred_vals.append(pred_sisf / 60)
        for pred_val in pred_vals:
            y_text = pred_val - 0.03 * (y_max - y_min)
            if y_text < y_min:
                y_min = y_text - 0.05 * (y_max - y_min)
            if y_text > y_max:
                y_max = y_text + 0.05 * (y_max - y_min)
        ax.set_ylim(y_min, y_max)

        # Draw deadline line with style depending on min/max
        deadline_minutes = int(deadline) / 60
        if deadline == min_deadline:
            ax.axhline(deadline_minutes, ls=":", color="red", label="Deadline B")
        elif deadline == max_deadline:
            ax.axhline(deadline_minutes, ls="--", color="red", label="Deadline A")
        else:
            ax.axhline(deadline_minutes, ls="-", color="red", label="Deadline")

        # Draw prediction lines and PE text
        pred_color = "orange"
        xticks = ax.get_xticks()
        # box_width already set above
        lines = []
        labels = []
        pe_texts = []
        x_centers = []
        pe_labels = []
        # Make prediction line as wide as the box plot using PathPatch vertices (like workflow_makespan.py)
        if len(ax.patches) >= len(order):
            for i in range(len(order)):
                box = ax.patches[i]
                path = box.get_path().vertices
                x_left = min(path[:, 0])
                x_right = max(path[:, 0])
                pred_val = pred_ljf_sisf / 60 if (only_ljf or only_sisf) else pred_vals[i]
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
                    y_min, y_max = ax.get_ylim()
                    y_text = pred_val - 0.03 * (y_max - y_min)
                    pe_texts.append(y_text)
                if i == 0:
                    lines.append(line[0])
                    labels.append("Prediction")
        else:
            # Fallback to old logic if box patches not available
            if only_ljf or only_sisf:
                if pred_ljf_sisf is not None:
                    x = xticks[0]
                    pred_val = pred_ljf_sisf / 60
                    line = ax.plot(
                        [x - box_width, x + box_width],
                        [pred_val] * 2,
                        ls="-", color=pred_color, label="Prediction"
                    )
                    lines.append(line[0])
                    labels.append("Prediction")
                    group_data = df[df["offloading"] == order[0]]["runtime_minutes"]
                    if not group_data.empty:
                        median_val = group_data.median()
                        pe = (pred_val - median_val) / median_val * 100
                        x_center = x
                        x_centers.append(x_center)
                        pe_labels.append(f"PE={pe:.2f}%")
                        y_min, y_max = ax.get_ylim()
                        y_text = pred_val - 0.03 * (y_max - y_min)
                        pe_texts.append(y_text)
            else:
                for i, pred_val in enumerate(pred_vals):
                    x = xticks[i]
                    line = ax.plot(
                        [x - box_width, x + box_width],
                        [pred_val] * 2,
                        ls="-", color=pred_color, label="Prediction" if i == 0 else None
                    )
                    if i == 0:
                        lines.append(line[0])
                        labels.append("Prediction")
                    group_data = df[df["offloading"] == order[i]]["runtime_minutes"]
                    if not group_data.empty:
                        median_val = group_data.median()
                        pe = (pred_val - median_val) / median_val * 100
                        x_center = x
                        x_centers.append(x_center)
                        pe_labels.append(f"PE={pe:.2f}%")
                        y_min, y_max = ax.get_ylim()
                        y_text = pred_val - 0.03 * (y_max - y_min)
                        pe_texts.append(y_text)
        # After collecting, expand y-axis if needed
        if pe_texts:
            y_min, y_max = ax.get_ylim()
            min_y_text = min(pe_texts)
            min_margin = 0.04 * (y_max - y_min)
            if min_y_text < y_min + min_margin:
                y_min = min_y_text - min_margin
            max_y_text = max(pe_texts)
            if max_y_text > y_max:
                margin = 0.05 * (y_max - y_min)
                y_max = max_y_text + margin
            # Ensure deadline is visible
            if deadline_minutes < y_min:
                y_min = deadline_minutes - 0.05 * (y_max - y_min)
            if deadline_minutes > y_max:
                y_max = deadline_minutes + 0.05 * (y_max - y_min)
            # Add extra margin for stained-glass workflow
            if plot_title == "stained-glass":
                margin = 0.08 * (y_max - y_min)
                y_min -= margin
                y_max += margin
            ax.set_ylim(y_min, y_max)
            y_min, y_max = ax.get_ylim()
            for i in range(len(pe_texts)):
                pred_val = pred_vals[i]
                # For stained-glass plot with deadline 4500, move PE text about 0.3 down
                if plot_title == "stained-glass" and str(deadline) == "4500":
                    y_text = pred_val - 0.3
                else:
                    y_text = pred_val - 0.03 * (y_max - y_min)
                ax.text(x_centers[i], y_text, pe_labels[i], color=pred_color, ha="center", va="top", fontsize=11)

        # Median lines removed from boxplots as requested
        # Add legend
        handles, handle_labels = ax.get_legend_handles_labels()
        # Remove duplicate 'Median' and 'Prediction' if present
        seen = set()
        new_handles = []
        new_labels = []
        for h, l in zip(handles, handle_labels):
            if l == 'Prediction' and 'Prediction' in seen:
                continue
            if l not in seen:
                new_handles.append(h)
                new_labels.append(l)
                seen.add(l)
        ax.legend(new_handles, new_labels, fontsize=12)

        plt.tight_layout(pad=1.0)
        plt.savefig(f"workflow_makespan_boxplot_{plot_title}_deadline_{deadline}.png", dpi=600, bbox_inches='tight')
        plt.savefig(f"workflow_makespan_boxplot_{plot_title}_deadline_{deadline}.pdf", bbox_inches='tight')
        plt.close()

        # Scatter plot (compact)
        plt.figure(figsize=(4, 4))
        np.random.seed(42)  # Set random seed for deterministic jitter
        ax2 = sns.stripplot(data=df, x="offloading", y="runtime_minutes", order=order, jitter=0.2, size=6, alpha=0.7, color="grey", dodge=True)
        ax2.set_xticklabels([label_map[label] for label in order], fontsize=12)
        ax2.set_xlabel("Offloading", fontsize=12)
        # ax2.set_title(f"{plot_title}", fontsize=14)  # Title removed
        ax2.set_ylabel("Workflow makespan (minutes)", fontsize=12)
        ax2.tick_params(axis='both', which='major', labelsize=12)
        # Center single-category lines for stained-glass
        if only_ljf or only_sisf:
            ax2.set_xlim(-0.5, 0.5)
            ax2.set_xticks([0])
            ax2.set_xticklabels([label_map[order[0]]], fontsize=12)

        # Expand y-axis if needed to ensure PE text is always visible
        y_min2, y_max2 = ax2.get_ylim()
        pred_vals2 = []
        if only_ljf or only_sisf:
            if pred_ljf_sisf is not None:
                pred_vals2.append(pred_ljf_sisf / 60)
        else:
            if pred_ljf is not None:
                pred_vals2.append(pred_ljf / 60)
            if pred_sisf is not None:
                pred_vals2.append(pred_sisf / 60)
        pe_texts2 = []
        x_centers2 = []
        pe_labels2 = []
        # Calculate y_text positions for PE and expand y-axis if needed
        for i, pred_val2 in enumerate(pred_vals2):
            if only_ljf or only_sisf:
                x2 = ax2.get_xticks()[0]
                group_data2 = df[df["offloading"] == order[0]]["runtime_minutes"]
            else:
                x2 = ax2.get_xticks()[i]
                group_data2 = df[df["offloading"] == order[i]]["runtime_minutes"]
            if not group_data2.empty:
                median_val2 = group_data2.median()
                pe2 = (pred_val2 - median_val2) / median_val2 * 100
                x_centers2.append(x2)
                pe_labels2.append(f"PE={pe2:.2f}%")
                y_text2 = pred_val2 - 0.03 * (y_max2 - y_min2)
                pe_texts2.append(y_text2)
        # Improved dynamic y-axis expansion for PE text
        if pe_texts2:
            min_y_text2 = min(pe_texts2)
            min_margin2 = 0.15 * (y_max2 - y_min2)
            if min_y_text2 < y_min2 + min_margin2:
                y_min2 = min_y_text2 - min_margin2
            max_y_text2 = max(pe_texts2)
            if max_y_text2 > y_max2:
                margin2 = 0.05 * (y_max2 - y_min2)
                y_max2 = max_y_text2 + margin2
            # Ensure deadline is visible
            if deadline_minutes < y_min2:
                y_min2 = deadline_minutes - 0.05 * (y_max2 - y_min2)
            if deadline_minutes > y_max2:
                y_max2 = deadline_minutes + 0.05 * (y_max2 - y_min2)
            # Add extra margin for stained-glass workflow
            if plot_title == "stained-glass":
                margin2 = 0.08 * (y_max2 - y_min2)
                y_min2 -= margin2
                y_max2 += margin2
            ax2.set_ylim(y_min2, y_max2)
            y_min2, y_max2 = ax2.get_ylim()
            for i in range(len(pe_texts2)):
                pred_val2 = pred_vals2[i]
                # For stained-glass plot with deadline 4500, move PE text about 0.3 down
                if plot_title == "stained-glass" and str(deadline) == "4500":
                    y_text2 = pred_val2 - 0.3
                else:
                    y_text2 = pred_val2 - 0.03 * (y_max2 - y_min2)
                ax2.text(x_centers2[i], y_text2, pe_labels2[i], color="orange", ha="center", va="top", fontsize=11)

        # Draw deadline line with style depending on min/max
        if deadline == min_deadline:
            ax2.axhline(deadline_minutes, ls=":", color="red", label="Deadline B")
        elif deadline == max_deadline:
            ax2.axhline(deadline_minutes, ls="--", color="red", label="Deadline A")
        else:
            ax2.axhline(deadline_minutes, ls="-", color="red", label="Deadline")

        # Draw prediction lines (orange) for LJF/SISF or both if available
        pred_lines2 = []
        xticks2 = ax2.get_xticks()
        # box_width and line_width already set above
        if only_ljf or only_sisf:
            if pred_ljf_sisf is not None:
                pred_line2 = ax2.plot(
                    [xticks2[0] - line_width, xticks2[0] + line_width],
                    [pred_ljf_sisf / 60] * 2,
                    ls="-", color="orange", label="Prediction"
                )
                pred_lines2.append(pred_line2[0])
        else:
            if pred_ljf is not None:
                pred_line2 = ax2.plot(
                    [xticks2[0] - line_width, xticks2[0] + line_width],
                    [pred_ljf / 60] * 2,
                    ls="-", color="orange", label="Prediction"
                )
                pred_lines2.append(pred_line2[0])
            if pred_sisf is not None:
                pred_line2 = ax2.plot(
                    [xticks2[1] - line_width, xticks2[1] + line_width],
                    [pred_sisf / 60] * 2,
                    ls="-", color="orange", label=None
                )
                pred_lines2.append(pred_line2[0])
        # Median lines
        median_color = "black"
        median_line_obj2 = None
        for i, group in enumerate(order):
            group_data2 = df[df["offloading"] == group]["runtime_minutes"]
            if not group_data2.empty:
                median_val2 = group_data2.median()
                median_line = ax2.plot(
                    [xticks2[i] - line_width, xticks2[i] + line_width],
                    [median_val2] * 2,
                    ls="-", color=median_color, label="Median" if median_line_obj2 is None else None
                )
                if median_line_obj2 is None:
                    median_line_obj2 = median_line[0]
        # Add legend
        handles2, handle_labels2 = ax2.get_legend_handles_labels()
        # Remove duplicate 'Median' and 'Prediction' if present
        seen2 = set()
        new_handles2 = []
        new_labels2 = []
        for h, l in zip(handles2, handle_labels2):
            if l == 'Prediction' and 'Prediction' in seen2:
                continue
            if l not in seen2:
                new_handles2.append(h)
                new_labels2.append(l)
                seen2.add(l)
        ax2.legend(new_handles2, new_labels2, fontsize=12)

        plt.tight_layout(pad=1.0)
        plt.savefig(f"workflow_makespan_scatter_{plot_title}_deadline_{deadline}.png", dpi=600, bbox_inches='tight')
        plt.savefig(f"workflow_makespan_scatter_{plot_title}_deadline_{deadline}.pdf", bbox_inches='tight')
        plt.close()

if __name__ == "__main__":
    main(
        "../../experimental-data/rna-seq-star-deseq2/experiments/offloading/ljf",
        "../../experimental-data/rna-seq-star-deseq2/experiments/offloading/sisf",
        plot_title="rna-seq-star-deseq2",
        predictions={
        "15000": {"LJF": 14962, "SISF": 14907},
        "18600": {"LJF": 18160, "SISF": 18256}
    }
    )
    main(
        None,
        "../../experimental-data/stained-glass/experiments/offloading/ljf_sisf",
        plot_title="stained-glass",
        predictions={
            "3900": {"SISF": 3584.69},
            "4500": {"SISF": 4329.99}
        }
    )
    main(
        "../../experimental-data/dna-seq-varlociraptor/experiments/offloading/ljf",
        "../../experimental-data/dna-seq-varlociraptor/experiments/offloading/sisf",
        plot_title="dna-seq-varlociraptor",
        predictions={
            "9600": {"LJF": 9560.64, "SISF": 9594.28},
            "10800": {"LJF": 10423.27, "SISF": 10723.59}
        }
    )