import json
import seaborn as sns
import matplotlib.pyplot as plt

from matplotlib.lines import Line2D
import pandas as pd

def plot(workflow, pcfo_no_offloaded_jobs, pcfo_prediction, deadline_a, deadline_b):
    # Helper to mark minimal x where y < deadline for a given strategy and deadline
    def mark_min_x_below_deadline(df, strategy, deadline):
        df_strat = df[(df["Strategy"] == strategy) & (df["Predicted Runtime"] < deadline)]
        if not df_strat.empty:
            min_x = df_strat["Offloaded Jobs"].min()
            y_val = df_strat[df_strat["Offloaded Jobs"] == min_x]["Predicted Runtime"].iloc[0]
            # Place a large orange circle at the same y position as the datapoint, but underneath the line plots
            plt.scatter(min_x, y_val, color="orange", marker="o", s=200, zorder=1)


    # Load JSON data
    with open(f"{workflow}/steps_until_deadline_smallest_input_size_first.json") as f:
        data1 = json.load(f)

    with open(f"{workflow}/steps_until_deadline_longest_job_first.json") as f:
        data2 = json.load(f)

    # Convert to DataFrames
    df1 = pd.DataFrame({
        "Offloaded Jobs": list(map(int, data1.keys())),
        "Predicted Runtime": [v/60 for v in list(data1.values())],
        "Strategy": "SISF"
    })

    df2 = pd.DataFrame({
        "Offloaded Jobs": list(map(int, data2.keys())),
        "Predicted Runtime": [v/60 for v in list(data2.values())],
        "Strategy": "LJF"
    })

    df3 = pd.DataFrame({
        "Offloaded Jobs": [pcfo_no_offloaded_jobs],
        "Predicted Runtime": [pcfo_prediction],
        "Strategy": "PEFO"
    })

    # Combine both DataFrames
    df = pd.concat([df1, df2, df3], ignore_index=True)

    # Plot using seaborn
    sns.set(style="whitegrid")
    plt.figure(figsize=(10, 4.5))

    # Custom color palette: SISF (light blue), LJF (dark blue)
    palette = ["#4FC3F7", "#0D47A1"]  # SISF, LJF

    # Only plot SISF and LJF as lines
    df_lines = df[df["Strategy"] != "PEFO"]
    sns.lineplot(data=df_lines, x="Offloaded Jobs", y="Predicted Runtime", hue="Strategy", marker="o", markersize=6, linewidth=1.5, palette=palette)

    # Plot PEFO as a single green dot
    df_pefo = df[df["Strategy"] == "PEFO"]
    if not df_pefo.empty:
        plt.scatter(df_pefo["Offloaded Jobs"], df_pefo["Predicted Runtime"], color="#43A047", s=60, label="PEFO", zorder=10)

    # Mark for both deadlines and both strategies (after all plot elements are drawn, before legend/save/show)
    for d in [deadline_a, deadline_b]:
        mark_min_x_below_deadline(df, "SISF", d)
        mark_min_x_below_deadline(df, "LJF", d)

    # Draw deadline lines and keep handles for custom legend
    deadline_a_label = "A"
    deadline_b_label = "B"
    line_a = plt.axhline(y=deadline_a, color='red', linestyle='--', linewidth=2)
    line_b = plt.axhline(y=deadline_b, color='red', linestyle=':', linewidth=2)

    plt.xlabel("Number of offloaded jobs", fontsize=15)
    plt.ylabel("Predicted makespan (minutes)", fontsize=15)
    # First legend (offloading strategies)
    handles, labels = plt.gca().get_legend_handles_labels()
    # Remove any existing PEFO from legend
    filtered = [(h, l) for h, l in zip(handles, labels) if l != "PEFO"]
    handles, labels = zip(*filtered) if filtered else ([], [])
    # Add PEFO to legend if present
    if not df_pefo.empty:
        handles = list(handles) + [Line2D([0], [0], marker='o', color='w', markerfacecolor="#43A047", markersize=10, label="PEFO")]
        labels = list(labels) + ["PEFO"]
    leg1 = plt.legend(handles, labels, title="Offloading strategy", fontsize=13, title_fontsize=14, loc='upper right', bbox_to_anchor=((0.85, 1) if workflow=="stained-glass" else (1, 1)))
    # Second legend (deadlines)
    deadline_handles = [Line2D([0], [0], color='red', linestyle='--', linewidth=2, label=deadline_a_label),
                       Line2D([0], [0], color='red', linestyle=':', linewidth=2, label=deadline_b_label)]
    leg2 = plt.legend(handles=deadline_handles, title="Deadlines", fontsize=13, title_fontsize=14, loc='upper right', bbox_to_anchor=((0.6, 1) if workflow=="stained-glass" else (0.75, 1)))
    plt.gca().add_artist(leg1)
    plt.xticks(fontsize=11)
    plt.yticks(fontsize=11)
    plt.tight_layout(pad=0.5)
    plt.savefig(f"workflow_offloading_steps_{workflow}.png", dpi=600, bbox_inches="tight")
    plt.savefig(f"workflow_offloading_steps_{workflow}.pdf", bbox_inches="tight")
    plt.show()

if __name__ == "__main__":
    plot("rna-seq-star-deseq2", pcfo_no_offloaded_jobs=54, pcfo_prediction=13349.72/60, deadline_a=310, deadline_b=250)
    plot("stained-glass", pcfo_no_offloaded_jobs=8, pcfo_prediction=3212.04/60, deadline_a=75, deadline_b=65)
    plot("dna-seq-varlociraptor", pcfo_no_offloaded_jobs=278, pcfo_prediction=8088.07 / 60, deadline_a=180, deadline_b=160)