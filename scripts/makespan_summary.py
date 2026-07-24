#!/usr/bin/env python3
import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

TIMESTAMP_FMT = "%a %b %d %H:%M:%S %Y"


def parse_args():
    p = argparse.ArgumentParser(description="Summarize predicted vs actual workflow makespan.")
    p.add_argument("--strategy", required=True, help="none|pefo|sisf|ljf or exact SOU strategy label")
    p.add_argument("--sou-predictions", required=True, help="Path to .sou/latest_workflow_predictions.json")
    p.add_argument("--runs-dir", required=True, help="Directory containing archived run_* folders")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    return p.parse_args()


def strategy_label_from_arg(strategy: str) -> str:
    aliases = {
        "none": "No offloading",
        "pefo": "Primary cluster fully occupied",
        "sisf": "Smallest input size first",
        "ljf": "Longest job first",
    }
    return aliases.get(strategy.lower(), strategy)


def read_prediction_seconds(pred_path: Path, strategy: str) -> float:
    label = strategy_label_from_arg(strategy)
    payload = json.loads(pred_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected predictions format in {pred_path}")

    matching = [entry for entry in payload if entry.get("offloading_strategy") == label]
    if not matching:
        raise ValueError(f"No prediction found for strategy '{strategy}' ({label}) in {pred_path}")

    return float(matching[-1]["prediction"])


def parse_runtime_from_metadata(meta_path: Path) -> float | None:
    kv = {}
    for line in meta_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    try:
        started = datetime.fromisoformat(kv["started_at"])
        finished = datetime.fromisoformat(kv["finished_at"])
        return (finished - started).total_seconds()
    except Exception:
        return None


def parse_runtime_from_log(log_path: Path) -> float | None:
    timestamps = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("[") and "]" in line:
            token = line[1:].split("]", 1)[0]
            try:
                timestamps.append(datetime.strptime(token, TIMESTAMP_FMT))
            except ValueError:
                continue
    if len(timestamps) < 2:
        return None
    return (max(timestamps) - min(timestamps)).total_seconds()


def collect_actual_makespans(runs_dir: Path) -> tuple[list[dict], list[str]]:
    rows = []
    failed = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir() or not run_dir.name.startswith("run_"):
            continue

        log_path = run_dir / "snakemake.log"
        if not log_path.exists():
            continue

        status = None
        runtime_s = None
        meta_path = run_dir / "run-metadata.txt"
        if meta_path.exists():
            meta_lines = meta_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in meta_lines:
                if line.startswith("status="):
                    status = line.split("=", 1)[1].strip()
                    break
            runtime_s = parse_runtime_from_metadata(meta_path)

        if runtime_s is None:
            runtime_s = parse_runtime_from_log(log_path)

        if runtime_s is None:
            continue

        if status not in {None, "0"}:
            failed.append(run_dir.name)

        rows.append({"run": run_dir.name, "actual_s": float(runtime_s), "status": status})

    return rows, failed


def summarize(prediction_s: float, rows: list[dict]) -> dict:
    if not rows:
        raise ValueError("No runs with measurable makespan found")

    actuals = [row["actual_s"] for row in rows]
    abs_errors = [abs(a - prediction_s) for a in actuals]
    ape = [(abs(a - prediction_s) / a * 100.0) for a in actuals if a > 0]

    actuals_sorted = sorted(actuals)
    n = len(actuals_sorted)
    if n % 2 == 1:
        median_actual = actuals_sorted[n // 2]
    else:
        median_actual = 0.5 * (actuals_sorted[n // 2 - 1] + actuals_sorted[n // 2])

    return {
        "prediction_s": prediction_s,
        "n_runs": n,
        "mean_actual_s": sum(actuals) / n,
        "median_actual_s": median_actual,
        "mae_s": sum(abs_errors) / n,
        "mape_percent": (sum(ape) / len(ape)) if ape else None,
        "runs": rows,
    }


def main():
    args = parse_args()
    prediction_s = read_prediction_seconds(Path(args.sou_predictions), args.strategy)
    rows, failed = collect_actual_makespans(Path(args.runs_dir))
    out = summarize(prediction_s, rows)
    out["strategy"] = args.strategy
    out["failed_runs"] = failed

    if args.format == "json":
        print(json.dumps(out, indent=2))
        return

    writer = csv.writer(__import__("sys").stdout)
    writer.writerow([
        "strategy",
        "prediction_s",
        "n_runs",
        "mean_actual_s",
        "median_actual_s",
        "mae_s",
        "mape_percent",
        "failed_runs",
    ])
    writer.writerow([
        out["strategy"],
        f"{out['prediction_s']:.6f}",
        out["n_runs"],
        f"{out['mean_actual_s']:.6f}",
        f"{out['median_actual_s']:.6f}",
        f"{out['mae_s']:.6f}",
        "" if out["mape_percent"] is None else f"{out['mape_percent']:.6f}",
        ";".join(out["failed_runs"]),
    ])


if __name__ == "__main__":
    main()
