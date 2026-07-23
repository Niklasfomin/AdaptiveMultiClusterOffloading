import logging
from pathlib import Path

import numpy as np
from scipy.stats import norm, pearsonr
from sklearn.linear_model import LinearRegression, BayesianRidge

from sou.snakemake_benchmarks import (
    collect_benchmark_files_of_all_runs,
    collect_benchmarks_per_rule,
    compute_ancestor_input_sizes,
    get_datapoints_by_rule,
    get_median_setup_time_per_rule,
)

logger = logging.getLogger("sou")

class Predictor:
    # Prediction always works based on multiple runs for reproducability
    def __init__(
        self, base_dir: str, corr_threshold: float, decision_model: str = "linear"
    ):
        valid_decision_models = {"linear", "bayesian", "median", "min", "max", "p90"}
        if decision_model not in valid_decision_models:
            raise ValueError(
                f"Unknown decision model '{decision_model}'. "
                f"Choose one of: {', '.join(sorted(valid_decision_models))}"
            )
        self.corr_threshold = corr_threshold
        self.decision_model = decision_model
        logger.info(f"Correlation threshold set to {self.corr_threshold:.2f}")

        base_path = Path(base_dir).resolve()
        if not base_path.is_dir():
            logger.error(f"Provided path is not a directory: {base_dir}")

        self.base_path = base_path
        self.runs = collect_benchmark_files_of_all_runs(self.base_path)
        collect_benchmarks_per_rule(self.runs)
        # TODO: Add function to compute the primary input sizes without combining them
        compute_ancestor_input_sizes(self.runs)

        self.datapoints_by_rule = get_datapoints_by_rule(self.runs)
        self.median_setup_times = get_median_setup_time_per_rule(self.runs)

        self.models = self.fit_models()

    # For the bayesian model, we store an estimator for each rule.
    # This is the main function that is used for fitting all models for all rules.
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
        if rule_name not in self.models or model_name not in self.models[rule_name]:
            logger.warning("Model '%s' not available for rule '%s'", model_name, rule_name)
            return None

        model = self.models[rule_name][model_name]
        if model is None:
            logger.warning("No model available for rule '%s'", rule_name)
            return None

        if "estimator" in model:
            estimator = model["estimator"]
            predictions, std_deviations = estimator.predict(
                np.array([[input_size]]), return_std=True
            )
            prediction = float(predictions[0])
            std_deviation = float(std_deviations[0])
            p50 = prediction
            p90 = max(0.0, prediction + norm.ppf(0.90) * std_deviation)
            p95 = max(0.0, prediction + norm.ppf(0.95) * std_deviation)
            lower_95 = max(0.0, prediction + norm.ppf(0.025) * std_deviation)
            upper_95 = max(0.0, prediction + norm.ppf(0.975) * std_deviation)
            logger.info(
                "Predicted runtime for rule '%s' with input size %s (%s): "
                "mean=%.4fs, std=%.4fs, p50=%.4fs, p90=%.4fs, p95=%.4fs, "
                "95%% interval=[%.4fs, %.4fs]",
                rule_name,
                input_size,
                self.decision_model,
                prediction,
                std_deviation,
                p50,
                p90,
                p95,
                lower_95,
                upper_95,
            )
            return prediction

        if "constant" in model:
            prediction = float(model["constant"])
            logger.info(
                "Predicted runtime for rule '%s' (%s baseline): %.4fs",
                rule_name,
                model["method"],
                prediction,
            )
            return prediction

        if "coef" in model:
            prediction = model["coef"] * input_size + model["intercept"]
            logger.info(
                "Predicted runtime for rule '%s' with input size %s (%s): %.4fs",
                rule_name,
                input_size,
                self.decision_model,
                prediction,
            )
            return prediction

        logger.info(
            "Predicted runtime for rule '%s' with input size %s (median fallback): %.4fs",
            rule_name,
            input_size,
            model["median"],
        )
        return model["median"]

    def fit_linear_model(self, x_values, y_values):
        assert len(x_values) == len(y_values), (
            "x and y values must have the same length"
        )
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
                "Weak correlation (%.2f). Returning median time: %ss",
                corr,
                median_time,
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

    def fit_baseline_model(self, y_values, method: str):
        if not y_values:
            logger.warning("No data points to fit %s baseline", method)
            return None

        aggregations = {
            "median": np.median,
            "min": np.min,
            "max": np.max,
            "p90": lambda values: np.percentile(values, 90),
        }
        value = float(aggregations[method](y_values))
        logger.info("Returning %s baseline: %.4fs", method, value)
        return {"constant": value, "method": method}

    # TODO: Refactor
    def fit_bayesian_model(self, x_values, y_values):
        assert len(x_values) == len(y_values), (
            "x and y values must have the same length"
        )
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
                "Weak correlation (%.2f). Returning median time: %ss",
                corr,
                median_time,
            )
            return {"median": median_time}

        model = BayesianRidge()
        X = np.array(x_values).reshape(-1, 1)
        y = np.array(y_values)
        model.fit(X, y)

        logger.info(
            f"Strong correlation ({corr:.2f}). Returning bayesian model "
            f"Coef={model.coef_[0]:.4f}, Intercept={model.intercept_:.4f}"
        )
        return {"estimator": model}

    def fit_decision_model(self, x_values, y_values):
        if self.decision_model == "bayesian":
            return self.fit_bayesian_model(x_values, y_values)
        if self.decision_model == "linear":
            return self.fit_linear_model(x_values, y_values)
        return self.fit_baseline_model(y_values, self.decision_model)

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

        logger.info("Fitting model_runtime (input size) with %s", self.decision_model)
        model_runtime = self.fit_decision_model(input_sizes, runtimes)
        logger.info(
            "Fitting model_runtime_apriori (initial input size of ancestors) with %s",
            self.decision_model,
        )
        model_runtime_apriori = self.fit_decision_model(initial_sizes, runtimes)
        logger.info("Fitting model_wall_time (input size) with %s", self.decision_model)
        model_wall_time = self.fit_decision_model(input_sizes, wall_times)
        logger.info(
            "Fitting model_wall_time_apriori (initial input size of ancestors) with %s",
            self.decision_model,
        )
        model_wall_time_apriori = self.fit_decision_model(
            initial_sizes, wall_times
        )
        return (
            model_runtime,
            model_runtime_apriori,
            model_wall_time,
            model_wall_time_apriori,
        )
