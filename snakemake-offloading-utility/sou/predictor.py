import logging
from pathlib import Path

import numpy as np
from scipy.stats import pearsonr
from sklearn.linear_model import LinearRegression

from sou.snakemake_benchmarks import (
    collect_benchmark_files_of_all_runs,
    collect_benchmarks_per_rule,
    compute_ancestor_input_sizes,
    get_datapoints_by_rule,
    get_median_setup_time_per_rule,
)

logger = logging.getLogger("sou")


class Predictor:
    def __init__(self, base_dir: str, corr_threshold: float):
        self.corr_threshold = corr_threshold
        logger.info(f"Correlation threshold set to {self.corr_threshold:.2f}")

        base_path = Path(base_dir).resolve()
        if not base_path.is_dir():
            logger.error(f"Provided path is not a directory: {base_dir}")

        self.base_path = base_path
        self.runs = collect_benchmark_files_of_all_runs(self.base_path)
        collect_benchmarks_per_rule(self.runs)
        compute_ancestor_input_sizes(self.runs)

        self.datapoints_by_rule = get_datapoints_by_rule(self.runs)
        self.median_setup_times = get_median_setup_time_per_rule(self.runs)

        self.models = self.fit_models()

    def fit_models(self):
        models = {}
        for rule_name, data_points in self.datapoints_by_rule.items():
            (
                model_runtime,
                model_runtime_apriori,
                model_wall_time,
                model_wall_time_apriori,
            ) = self.analyze_rule(rule_name, data_points)
            models[rule_name] = {
                "model_runtime": model_runtime,
                "model_runtime_apriori": model_runtime_apriori,
                "model_wall_time": model_wall_time,
                "model_wall_time_apriori": model_wall_time_apriori,
            }
        return models

    def predict(self, model_name: str, rule_name: str, input_size: float):
        if rule_name not in self.models:
            logger.warning(f"Rule '{rule_name}' not found in models")
            return None
        if model_name not in [
            "model_runtime",
            "model_runtime_apriori",
            "model_wall_time",
            "model_wall_time_apriori",
        ]:
            logger.error(
                f'Model "{model_name}" not found. Available models: "model_runtime", "model_runtime_apriori", "model_wall_time", "model_wall_time_apriori"'
            )
            return None

        model = self.models[rule_name][model_name]
        if "coef" in model:
            prediction = model["coef"] * input_size + model["intercept"]
            logger.info(
                f"Predicted runtime for rule '{rule_name}' with input size {input_size} (linear model): {prediction:.4f}s"
            )
            return prediction
        elif "median" in model:
            logger.info(
                f"Predicted runtime for rule '{rule_name}' with input size {input_size} (median): {model['median']:.4f}s"
            )
            return model["median"]
        else:
            logger.warning(f"No model available for rule '{rule_name}'")
            return None

    def fit_model(self, x_values, y_values):
        assert len(x_values) == len(
            y_values
        ), "x and y values must have the same length"
        if len(x_values) == 0:
            logger.warning("No data points to fit model")
            return
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

        model = LinearRegression()
        X = np.array(x_values).reshape(-1, 1)
        y = np.array(y_values)
        model.fit(X, y)

        logger.info(
            f"Strong correlation ({corr:.2f}). Returning linear model "
            f"Coef={model.coef_[0]:.4f}, Intercept={model.intercept_:.4f}"
        )
        return {"coef": model.coef_[0], "intercept": model.intercept_}

    def analyze_rule(self, rule_name, data_points):
        runtimes = [dp.runtime for dp in data_points]
        wall_times = [dp.wall_time for dp in data_points]
        input_sizes = [dp.total_input_size for dp in data_points]
        initial_sizes = [dp.total_initial_input_size_ancestors for dp in data_points]

        logger.info(f"Analyzing rule '{rule_name}' with {len(data_points)} data points")
        logger.debug(f"runtimes: {runtimes}")
        logger.debug(f"wall times: {wall_times}")
        logger.debug(f"input sizes: {input_sizes}")
        logger.debug(f"initial input sizes of ancestors: {initial_sizes}")

        logger.info(f"Fitting model_runtime (input size)")
        model_runtime = self.fit_model(input_sizes, runtimes)
        logger.info(f"Fitting model_runtime_apriori (initial input size of ancestors)")
        model_runtime_apriori = self.fit_model(initial_sizes, runtimes)
        logger.info(f"Fitting model_wall_time (input size)")
        model_wall_time = self.fit_model(input_sizes, wall_times)
        logger.info(
            f"Fitting model_wall_time_apriori (initial input size of ancestors)"
        )
        model_wall_time_apriori = self.fit_model(initial_sizes, wall_times)
        return (
            model_runtime,
            model_runtime_apriori,
            model_wall_time,
            model_wall_time_apriori,
        )
