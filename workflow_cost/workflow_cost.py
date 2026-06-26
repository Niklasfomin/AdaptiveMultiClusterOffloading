import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd


def plot(cost_dict, workflow):
    categories = []
    data = []
    plot_info = []
    for cat, value in cost_dict.items():
        if cat == "PEFO":
            categories.append(cat)
            for val in value["real_values"]:
                data.append({"Category": cat, "Cost": val})
            plot_info.append({
                "name": cat,
                "prediction": value["prediction"],
                "real_values": value["real_values"]
            })
        else:
            for deadline, subval in value.items():
                # Insert line break before (XXX min)
                cat_name = f"{cat}\n({deadline})"
                categories.append(cat_name)
                for val in subval["real_values"]:
                    data.append({"Category": cat_name, "Cost": val})
                plot_info.append({
                    "name": cat_name,
                    "prediction": subval["prediction"],
                    "real_values": subval["real_values"]
                })
    df = pd.DataFrame(data)

    # Print median and prediction for every category
    for info in plot_info:
        name = info["name"]
        pred = info["prediction"]
        real_vals = info["real_values"]
        median_val = np.median(real_vals)
        pe = (pred - median_val) / median_val * 100 if median_val != 0 else float('nan')
        print(f"{workflow} - {name}: Median = {median_val:.3f}, Prediction = {pred:.3f}, PE = {pe:.2f}%")

    sns.set(style="whitegrid")
    plt.figure(figsize=(4, 4))
    ax = sns.swarmplot(data=df, x="Category", y="Cost", order=categories, size=6, alpha=0.7, color="grey", dodge=True)
    ax.set_xlabel("Offloading", fontsize=12)
    ax.set_ylabel("Workflow cost ($)", fontsize=12)
    ax.set_title(workflow, fontsize=14)
    ax.tick_params(axis='both', which='major', labelsize=10)
    # Set y-axis to start at 0
    y_min, y_max = ax.get_ylim()
    # For dna-seq-varlociraptor, extend y-axis to ensure all prediction lines are visible
    if workflow == "dna-seq-varlociraptor":
        # Find the highest prediction value
        max_pred = max([info["prediction"] for info in plot_info])
        # Add margin above the highest prediction value
        new_y_max = max(y_max, max_pred + 0.2)
        ax.set_ylim(0, new_y_max)
    else:
        ax.set_ylim(0, y_max)

    # Draw prediction and median lines for each category
    pred_color = "orange"
    median_color = "black"
    xticks = ax.get_xticks()
    # Use narrower lines for stained-glass (3 categories), otherwise default
    if workflow == "stained-glass":
        box_width = 0.28
    else:
        box_width = 0.4
    lines = []
    labels = []
    for i, info in enumerate(plot_info):
        # Prediction line
        pred_val = info["prediction"]
        pred_line = ax.plot(
            [xticks[i] - box_width, xticks[i] + box_width],
            [pred_val] * 2,
            ls="-", color=pred_color, label="Prediction" if i == 0 else None
        )
        if i == 0:
            lines.append(pred_line[0])
            labels.append("Prediction")
        # Median line
        real_vals = info["real_values"]
        median_val = np.median(real_vals)
        median_line = ax.plot(
            [xticks[i] - box_width, xticks[i] + box_width],
            [median_val] * 2,
            ls="-", color=median_color, label="Median" if i == 0 else None
        )
        if i == 0:
            lines.append(median_line[0])
            labels.append("Median")
        # Prediction error (PE) annotation
        pe = (pred_val - median_val) / median_val * 100 if median_val != 0 else float('nan')
        x_center = xticks[i]
        y_min, y_max = ax.get_ylim()
        # Move PE text slightly down for 'LJF (160 min)' in dna-seq-varlociraptor
        if workflow == "dna-seq-varlociraptor" and "160 min" in info["name"] and "LJF" in info["name"]:
            y_text = pred_val - 0.05 * (y_max - y_min)
        else:
            y_text = pred_val - 0.03 * (y_max - y_min)
        # Expand y-axis if needed to ensure PE text is visible
        min_margin = 0.04 * (y_max - y_min)
        if y_text < y_min + min_margin:
            y_min = y_text - min_margin
            ax.set_ylim(y_min, y_max)
            y_min, y_max = ax.get_ylim()
            y_text = pred_val - 0.03 * (y_max - y_min)
        if y_text > y_max:
            y_max = y_text + min_margin
            ax.set_ylim(y_min, y_max)
            y_min, y_max = ax.get_ylim()
            y_text = pred_val - 0.03 * (y_max - y_min)
        ax.text(x_center, y_text, f"PE={pe:.2f}%", color=pred_color, ha="center", va="top", fontsize=10)
    if labels:
        ax.legend(lines, labels, fontsize=11)
    plt.tight_layout(pad=1.0)
    plt.savefig(f"workflow_cost_scatter_{workflow}.png", dpi=600, bbox_inches='tight')
    plt.savefig(f"workflow_cost_scatter_{workflow}.pdf", bbox_inches='tight')
    plt.close()


if __name__ == "__main__":
    cost_dict_rna = {
        "PEFO": {"prediction": 6.9651, "real_values": [5.335880029537998,
                                                       5.222112956347999,
                                                       5.1509966449940014,
                                                       4.775850515538002,
                                                       4.557644137071999,
                                                       ]},
        "LJF": {
            "310 min": {"prediction": 4.0279, "real_values": [2.064673324688,
                                                              2.068960173332,
                                                              2.06245051428,
                                                              2.064514552516,
                                                              2.064990869032
                                                              ]},
            "250 min": {"prediction": 6.4227, "real_values": [3.867055021232,
                                                              3.8876954035919993,
                                                              3.7944961386279994,
                                                              3.5033079751799994,
                                                              3.8054514184959998
                                                              ]}
        },
        "SISF": {
            "310 min": {"prediction": 3.3766, "real_values": [1.7012362812599995,
                                                              1.7143094198820001,
                                                              1.7163632358239997,
                                                              1.7299661466340008,
                                                              1.7146780756960012,
                                                              ]},
            "250 min": {"prediction": 6.2380, "real_values": [4.831326299682,
                                                              4.758353491733998,
                                                              4.836912013227999,
                                                              4.831850345723999,
                                                              4.814564162590001
                                                              ]}
        }
    }
    plot(cost_dict_rna, workflow="rna-seq-star-deseq2")

    cost_dict_stained = {
        "PEFO": {"prediction": 0.7047, "real_values": [0.740081207912,
                                                       0.734171904112,
                                                       0.7362992534799999,
                                                       0.7334627876559999,
                                                       0.765136656024,
                                                       ]},
        "LJF/SISF": {
            "75 min": {"prediction": 0.1762, "real_values": [0.21746237984,
                                                             0.21793512414400001,
                                                             0.21746237984,
                                                             0.219116984904,
                                                             0.21793512414400001,

                                                             ]},
            "65 min": {"prediction": 0.5285, "real_values": [0.584075587592,
                                                             0.5904576356960001,
                                                             0.582893726832,
                                                             0.58620293696,
                                                             0.589984891392,
                                                             ]}
        },
        # "SISF": {
        #     "75 min": {"prediction": 0.1762, "real_values": [0.21746237984,
        #                                                      0.21793512414400001,
        #                                                      0.21746237984,
        #                                                      0.219116984904,
        #                                                      0.21793512414400001,
        #
        #                                                      ]},
        #     "65 min": {"prediction": 0.5285, "real_values": [0.584075587592,
        #                                                      0.5904576356960001,
        #                                                      0.582893726832,
        #                                                      0.58620293696,
        #                                                      0.589984891392,
        #                                                      ]}
        # },
    }
    plot(cost_dict_stained, workflow="stained-glass")

    cost_dict_dna = {
        "PEFO": {"prediction": 3.3875, "real_values": [4.226432502284001,
                                                       4.096830750936002,
                                                       4.077159795534003,
                                                       4.052996317598001,
                                                       4.2539055885620005
                                                       ]},
        "LJF": {
            "180 min": {"prediction": 1.2642, "real_values": [0.70803949542,
                                                              0.6677011591160001,
                                                              0.6991837052020001,
                                                              0.7150309087500001,
                                                              0.7309204845,
                                                              ]},
            "160 min": {"prediction": 2.1221, "real_values": [1.155023854318,
                                                              1.0710421499540004,
                                                              1.9408587126100005,
                                                              2.2909378455340006,
                                                              2.4722484978920005
                                                              ]}
        },
        "SISF": {
            "180 min": {"prediction": 3.5833, "real_values": [1.4825918996639995,
                                                              1.448553721294,
                                                              0.7356267669619996,
                                                              1.458971860913999,
                                                              0.822579070672
                                                              ]},
            "160 min": {"prediction": 6.6773, "real_values": [2.4741336097400013,
                                                              2.7613608450840026,
                                                              2.4001986781460016,
                                                              2.653582596246,
                                                              3.0073547451360034,
                                                              ]}
        }
    }
    plot(cost_dict_dna, workflow="dna-seq-varlociraptor")
