import argparse
import logging
import math
import os
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from scipy.stats import pearsonr
from sklearn.linear_model import BayesianRidge, LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
SOU_PATH = REPO_ROOT / "snakemake-offloading-utility"
sys.path.insert(0, str(SOU_PATH))

from sou.snakemake_benchmarks import (  # noqa: E402
    collect_benchmark_files_of_all_runs,
    collect_benchmarks_per_rule,
    compute_ancestor_input_sizes,
    get_datapoints_by_rule,
)

logger = logging.getLogger("model-evaluation")

BAYESIAN_MIN_N = 5
BAYESIAN_MIN_IMPROVEMENT_PERCENT = 10.0
BAYESIAN_COVERAGE_MIN_PERCENT = 50.0
BAYESIAN_COVERAGE_MAX_PERCENT = 100.0
BAYESIAN_MAX_NLPD = 10.0


class PredictorModel:
    def __init__(
        self,
        corr_threshold: float,
        linear_model: str = "sklearn",
    ):
        self.corr_threshold = corr_threshold
        self.linear_model = linear_model

    def fit_model(self, x_values, y_values):
        assert len(x_values) == len(y_values), (
            "x and y values must have the same length"
        )
        if len(x_values) == 0:
            logger.warning("No data points to fit model")
            return None
        if len(x_values) == 1:
            logger.info(f"Only one data point. Returning time: {y_values[0]}s")
            return {"median": y_values[0]}
        if len(set(x_values)) == 1 or len(set(y_values)) == 1:
            median_time = np.median(y_values)
            logger.info(
                f"All x values or all y values are the same. Cannot calculate correlation. Returning median time: {median_time}s"
            )
            return {"median": median_time}

        corr, _ = pearsonr(x_values, y_values)
        if np.isnan(corr):
            median_time = np.median(y_values)
            logger.warning(
                f"Correlation is NaN. Returning median time : {median_time}s"
            )
            return {"median": median_time}

        if corr < self.corr_threshold:
            median_time = np.median(y_values)
            logger.info(
                f"Weak correlation ({corr:.2f}). Returning median time: {median_time}s"
            )
            return {"median": median_time}

        if self.linear_model == "bayesian-ridge":
            model = BayesianRidge(fit_intercept=True)
        else:
            model = LinearRegression()
        X = np.array(x_values).reshape(-1, 1)
        y = np.array(y_values)
        model.fit(X, y)

        logger.info(
            f"Strong correlation ({corr:.2f}). Returning linear model "
            f"Coef={model.coef_[0]:.4f}, Intercept={model.intercept_:.4f}"
        )
        return {
            "coef": model.coef_[0],
            "intercept": model.intercept_,
            "linear_model": self.linear_model,
            "estimator": model,
        }

    def predict(self, model, input_size):
        if "coef" in model:
            model_type = model.get("linear_model", "linear")
            if model_type == "bayesian-ridge":
                mean, std = model["estimator"].predict(
                    np.array([input_size]).reshape(-1, 1), return_std=True
                )
                return float(mean[0]), model_type, float(std[0])
            prediction = model["coef"] * input_size + model["intercept"]
            return prediction, model_type, None
        return model["median"], "median", None


def resolve_runs(path: str) -> list[Path]:
    path = Path(path).resolve()
    if path.is_file():
        path = path.parent
    if (path / "benchmarks").is_dir():
        return [path]
    return sorted(p for p in path.iterdir() if (p / "benchmarks").is_dir())


def load_datapoints(path: str):
    runs = []
    for run_path in resolve_runs(path):
        runs.extend(
            collect_benchmark_files_of_all_runs(
                run_path.parent if len(resolve_runs(path)) > 1 else run_path.parent
            )
        )
        break

    selected = {p.resolve() for p in resolve_runs(path)}
    runs = [run for run in runs if run.run_path.resolve() in selected]
    collect_benchmarks_per_rule(runs)
    compute_ancestor_input_sizes(runs)
    return get_datapoints_by_rule(runs), len(runs)


def get_xy(data_points, input_mode: str, target: str):
    if input_mode == "total":
        x = [dp.total_input_size for dp in data_points]
    elif input_mode == "ancestor":
        x = [dp.total_initial_input_size_ancestors for dp in data_points]
    else:
        raise ValueError(f"Unsupported input mode: {input_mode}")

    if target == "runtime":
        y = [dp.runtime for dp in data_points]
    elif target == "wall-time":
        y = [dp.wall_time for dp in data_points]
    else:
        raise ValueError(f"Unsupported target: {target}")

    pairs = [(xi, yi) for xi, yi in zip(x, y) if xi is not None and yi is not None]
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _evaluate_rule_cv(
    x_values,
    y_values,
    corr_threshold,
    linear_model="sklearn",
):
    predictor = PredictorModel(
        corr_threshold,
        linear_model=linear_model,
    )
    rows = []
    n = len(x_values)
    if n == 0:
        return rows

    for i in range(n):
        train_x = x_values[:i] + x_values[i + 1 :]
        train_y = y_values[:i] + y_values[i + 1 :]
        test_x = x_values[i]
        test_y = y_values[i]

        model = predictor.fit_model(train_x, train_y)
        if model is None:
            continue
        prediction, model_type, predicted_std = predictor.predict(model, test_x)
        lower_95 = (
            prediction - 1.96 * predicted_std if predicted_std is not None else None
        )
        upper_95 = (
            prediction + 1.96 * predicted_std if predicted_std is not None else None
        )
        covered_95 = (
            lower_95 <= test_y <= upper_95
            if lower_95 is not None and upper_95 is not None
            else None
        )
        baseline = float(np.median(train_y)) if train_y else test_y
        rows.append(
            {
                "actual": test_y,
                "predicted": prediction,
                "predicted_std": predicted_std,
                "lower_95": lower_95,
                "upper_95": upper_95,
                "covered_95": covered_95,
                "baseline": baseline,
                "model_type": model_type,
                "input_size_mb": test_x,
            }
        )
    return rows


def _evaluate_median_baseline_rows(x_values, y_values):
    rows = []
    n = len(x_values)
    for i in range(n):
        train_y = y_values[:i] + y_values[i + 1 :]
        test_x = x_values[i]
        test_y = y_values[i]
        baseline = float(np.median(train_y)) if train_y else test_y
        rows.append(
            {
                "actual": test_y,
                "predicted": baseline,
                "predicted_std": None,
                "lower_95": None,
                "upper_95": None,
                "covered_95": None,
                "baseline": baseline,
                "model_type": "median",
                "input_size_mb": test_x,
            }
        )
    return rows


def evaluate_rule(
    x_values,
    y_values,
    corr_threshold,
    linear_model="sklearn",
):
    if linear_model != "bayesian-ridge":
        return _evaluate_rule_cv(
            x_values,
            y_values,
            corr_threshold,
            linear_model=linear_model,
        )

    n = len(x_values)
    if n < BAYESIAN_MIN_N:
        return _evaluate_median_baseline_rows(x_values, y_values)

    bayesian_rows = _evaluate_rule_cv(
        x_values,
        y_values,
        corr_threshold,
        linear_model="bayesian-ridge",
    )
    linear_rows = _evaluate_rule_cv(
        x_values,
        y_values,
        corr_threshold,
        linear_model="sklearn",
    )
    if not bayesian_rows or not linear_rows:
        return _evaluate_median_baseline_rows(x_values, y_values)

    actual = [r["actual"] for r in bayesian_rows]
    baseline = [r["baseline"] for r in bayesian_rows]
    bayesian_pred = [r["predicted"] for r in bayesian_rows]
    linear_pred = [r["predicted"] for r in linear_rows]
    bayesian_std = [r["predicted_std"] for r in bayesian_rows]

    baseline_mae = mean_absolute_error(actual, baseline)
    best_cv_mae = min(
        mean_absolute_error(actual, bayesian_pred),
        mean_absolute_error(actual, linear_pred),
    )
    improvement_percent = (
        (baseline_mae - best_cv_mae) / baseline_mae * 100 if baseline_mae else 0.0
    )

    coverage_values = [
        r["covered_95"] for r in bayesian_rows if r["covered_95"] is not None
    ]
    coverage_95 = np.mean(coverage_values) * 100 if coverage_values else math.nan
    nlpd = gaussian_nlpd(actual, bayesian_pred, bayesian_std)

    use_bayesian = (
        improvement_percent >= BAYESIAN_MIN_IMPROVEMENT_PERCENT
        and np.isfinite(coverage_95)
        and BAYESIAN_COVERAGE_MIN_PERCENT
        <= coverage_95
        <= BAYESIAN_COVERAGE_MAX_PERCENT
        and np.isfinite(nlpd)
        and nlpd <= BAYESIAN_MAX_NLPD
    )
    return (
        bayesian_rows
        if use_bayesian
        else _evaluate_median_baseline_rows(x_values, y_values)
    )


def safe_mape(actual, predicted):
    values = [abs((p - a) / a) for a, p in zip(actual, predicted) if a]
    return float(np.mean(values) * 100) if values else math.nan


def gaussian_nlpd(actual, predicted, std):
    values = []
    for a, p, s in zip(actual, predicted, std):
        if s is None or not np.isfinite(s) or s <= 0:
            continue
        variance = s**2
        values.append(
            0.5 * math.log(2 * math.pi * variance) + ((a - p) ** 2) / (2 * variance)
        )
    return float(np.mean(values)) if values else math.nan


def summarize_rule(rule, rows):
    actual = [r["actual"] for r in rows]
    predicted = [r["predicted"] for r in rows]
    predicted_std = [r["predicted_std"] for r in rows]
    baseline = [r["baseline"] for r in rows]
    if not actual:
        return None

    mae = mean_absolute_error(actual, predicted)
    baseline_mae = mean_absolute_error(actual, baseline)
    rmse = math.sqrt(mean_squared_error(actual, predicted))
    mape = safe_mape(actual, predicted)
    r2 = (
        r2_score(actual, predicted)
        if len(actual) > 1 and len(set(actual)) > 1
        else math.nan
    )
    improvement = (
        ((baseline_mae - mae) / baseline_mae * 100) if baseline_mae else math.nan
    )
    linear_folds = sum(r["model_type"] != "median" for r in rows)
    bayesian_rows = [r for r in rows if r["model_type"] == "bayesian-ridge"]
    coverage_95 = (
        np.mean([r["covered_95"] for r in bayesian_rows]) * 100
        if bayesian_rows
        else math.nan
    )
    mean_std = (
        np.mean([r["predicted_std"] for r in bayesian_rows])
        if bayesian_rows
        else math.nan
    )
    mean_interval_width_95 = (
        np.mean([r["upper_95"] - r["lower_95"] for r in bayesian_rows])
        if bayesian_rows
        else math.nan
    )
    nlpd = gaussian_nlpd(actual, predicted, predicted_std)

    return {
        "rule": rule,
        "n": len(rows),
        "mae": mae,
        "rmse": rmse,
        "mape_percent": mape,
        "r2": r2,
        "median_baseline_mae": baseline_mae,
        "mae_improvement_percent": improvement,
        "linear_folds": linear_folds,
        "median_folds": len(rows) - linear_folds,
        "bayesian_folds": len(bayesian_rows),
        "mean_predicted_std": mean_std,
        "coverage_95_percent": coverage_95,
        "mean_interval_width_95": mean_interval_width_95,
        "gaussian_nlpd": nlpd,
    }


def evaluate(
    path,
    input_mode,
    target,
    corr_threshold,
    linear_model="sklearn",
):
    datapoints_by_rule, run_count = load_datapoints(path)
    prediction_rows = []
    metric_rows = []

    for rule, data_points in sorted(datapoints_by_rule.items()):
        x_values, y_values = get_xy(data_points, input_mode, target)
        rows = evaluate_rule(
            x_values,
            y_values,
            corr_threshold,
            linear_model=linear_model,
        )
        for row in rows:
            row["rule"] = rule
            prediction_rows.append(row)
        summary = summarize_rule(rule, rows)
        if summary:
            metric_rows.append(summary)

    return pd.DataFrame(prediction_rows), pd.DataFrame(metric_rows), run_count


def plot_metrics(metrics, workflow, input_mode, target):
    if metrics.empty:
        return
    ordered = metrics.sort_values("mae_improvement_percent", ascending=False)
    plt.figure(figsize=(10, 5))
    ax = sns.barplot(data=ordered, x="rule", y="mae_improvement_percent")
    ax.axhline(0, color="black", linewidth=1)
    ax.set_ylabel("MAE improvement over median baseline [%]")
    ax.set_xlabel("Rule")
    ax.set_title(f"{workflow}: model improvement ({input_mode}, {target})")
    plt.xticks(rotation=60, ha="right")
    plt.tight_layout()
    plt.savefig(
        SCRIPT_DIR
        / f"linear_model_eval_{workflow}_{input_mode}_{target}_mae_improvement.png",
        dpi=300,
        bbox_inches="tight",
    )
    plt.close()


def plot_predictions(predictions, workflow, input_mode, target):
    if predictions.empty:
        return
    g = sns.FacetGrid(
        predictions, col="rule", col_wrap=3, sharex=False, sharey=False, height=3
    )
    g.map_dataframe(sns.scatterplot, x="actual", y="predicted", hue="model_type")
    for ax in g.axes.flat:
        xmin, xmax = ax.get_xlim()
        ymin, ymax = ax.get_ylim()
        lo = min(xmin, ymin)
        hi = max(xmax, ymax)
        ax.plot([lo, hi], [lo, hi], color="black", linewidth=1, linestyle="--")
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
    g.set_axis_labels("Actual runtime [s]", "Predicted runtime [s]")
    g.fig.suptitle(f"{workflow}: predicted vs actual ({input_mode}, {target})", y=1.02)
    g.tight_layout()
    g.savefig(
        SCRIPT_DIR
        / f"linear_model_eval_{workflow}_{input_mode}_{target}_predicted_vs_actual.png",
        dpi=300,
        bbox_inches="tight",
    )
    plt.close(g.fig)


def print_summary(metrics, model):
    if metrics.empty:
        print("No metrics produced")
        return
    useful = metrics[metrics["mae_improvement_percent"] > 0]
    strong = metrics[metrics["mae_improvement_percent"] >= 10]
    linear = metrics[metrics["linear_folds"] > 0]
    print("\nModel evaluation summary")
    print(f"rules evaluated: {len(metrics)}")
    print(f"rules with any linear fold: {len(linear)}/{len(metrics)}")
    print(f"rules better than median baseline: {len(useful)}/{len(metrics)}")
    print(f"rules with >=10% MAE improvement: {len(strong)}/{len(metrics)}")
    print("\nRecommended metrics to inspect:")
    print("- MAE improvement over median baseline: primary decision metric")
    print("- RMSE: penalizes large errors")
    print("- MAPE: relative error, useful across differently scaled rules")
    print("- R²: explained variance, diagnostic only for small n")
    if model == "bayesian":
        print(
            "- coverage_95_percent: Bayesian 95% interval calibration; ideal is near 95%"
        )
        print(
            "- mean_predicted_std / mean_interval_width_95: Bayesian uncertainty magnitude"
        )
        print("- gaussian_nlpd: probabilistic predictive quality; lower is better")
    print("\nPer-rule metrics:")
    table = metrics.sort_values("mae_improvement_percent", ascending=False)
    if model != "bayesian":
        drop_cols = [
            "bayesian_folds",
            "mean_predicted_std",
            "coverage_95_percent",
            "mean_interval_width_95",
            "gaussian_nlpd",
        ]
        table = table.drop(columns=[c for c in drop_cols if c in table.columns])
    print(table.to_string(index=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        required=True,
        help="single run snakemake.log/run directory or training-runs directory",
    )
    parser.add_argument("--workflow", default="stained-glass")
    parser.add_argument("--input-mode", choices=["total", "ancestor"], default="total")
    parser.add_argument("--target", choices=["runtime", "wall-time"], default="runtime")
    parser.add_argument("--corr-threshold", type=float, default=0.8)
    parser.add_argument(
        "--model",
        choices=["linear", "bayesian"],
        default="linear",
        help="prediction model to evaluate",
    )
    args = parser.parse_args()

    linear_model = "bayesian-ridge" if args.model == "bayesian" else "sklearn"
    predictions, metrics, _ = evaluate(
        args.input,
        args.input_mode,
        args.target,
        args.corr_threshold,
        linear_model=linear_model,
    )
    suffix = f"_{args.model}"
    metrics_path = (
        SCRIPT_DIR
        / f"linear_model_eval_{args.workflow}_{args.input_mode}_{args.target}{suffix}_metrics.csv"
    )
    predictions_path = (
        SCRIPT_DIR
        / f"linear_model_eval_{args.workflow}_{args.input_mode}_{args.target}{suffix}_predictions.csv"
    )
    metrics.to_csv(metrics_path, index=False)
    predictions.to_csv(predictions_path, index=False)
    plot_metrics(metrics, args.workflow, args.input_mode, f"{args.target}{suffix}")
    plot_predictions(
        predictions, args.workflow, args.input_mode, f"{args.target}{suffix}"
    )
    print_summary(metrics, args.model)
    print(f"\nwrote {metrics_path}")
    print(f"wrote {predictions_path}")


if __name__ == "__main__":
    main()
