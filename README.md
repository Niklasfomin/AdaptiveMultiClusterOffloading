# Adaptive Multi-Cluster Offloading

Research code and artifacts for adaptive offloading of Snakemake workflow jobs across multiple cluster environments.

## Components

- `snakemake-offloading-utility/` — `sou` CLI/TUI wrapper for selecting offloading configurations.
- `snakemake-offloader/` — Snakemake executor plugin that dispatches jobs to primary and secondary environments.
- `snakemake-executor-plugin-kubernetes/` — adapted Kubernetes executor plugin used by the offloader.
- `py-lotaru/` — runtime prediction and analysis code.
- `experimental-data/` — experiment inputs/results.
- `plots/` — plotting outputs and related material.
- `scripts/` — setup, workflow download, and helper scripts.
- `two_cluster.yaml` — example kubeconfig template for a two-cluster setup.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -r requirements.txt
python -m pip install ./snakemake-executor-plugin-kubernetes ./snakemake-offloader ./snakemake-offloading-utility
```

## Run with SOU

From a Snakemake workflow directory, replace `snakemake` with `sou`:

```bash
sou <snakemake-options>
```

SOU opens the TUI and writes the selected offloading job set for the offloader.

## Run Snakemake directly

```bash
snakemake \
  --executor offloader \
  --offloader-primary-comp-env kubernetes:<primary-context> \
  --offloader-secondary-comp-env kubernetes:<secondary-context> \
  --offloader-jobs <job-id-list> \
  <snakemake-options>
```

Example:

```bash
snakemake \
  --executor offloader \
  --offloader-primary-comp-env kubernetes:primary \
  --offloader-secondary-comp-env kubernetes:secondary \
  --offloader-jobs 1,4,5
```

## Lotaru

Run Lotaru locally from `py-lotaru/` so it can find `data/traces` and `data/benchmarks`:

```bash
cd py-lotaru
python -m pip install -r requirements.txt
```

List available scripts:

```bash
python -m lotaru list
```

Show options for a script:

```bash
python -m lotaru describe results_csv
```

Run a simple prediction error summary:

```bash
python -m lotaru run node_error
```

Write predictions to CSV:

```bash
python -m lotaru run results_csv \
  --estimator lotaru-g \
  --experiment-number 1 \
  --resource-x taskinputsizeuncompressed \
  --resource-y realtime \
  --output-file results.csv
```

Generate result CSVs for multiple resources:

```bash
python -m lotaru run all_results_csv \
  --output-folder results \
  --resource-y realtime \
  --resource-y rss
```

## Workflow data

Use the scripts in `scripts/` to clone workflows and download datasets, for example:

```bash
bash scripts/clone-workflows.sh
bash scripts/download-stained-glass-arabidopsis.sh
```

## Notes

- Kubernetes contexts must be available in your kubeconfig.
- `sou-config.yaml` is expected in the workflow working directory; see `snakemake-offloading-utility/sou-config.yaml`.
- Historical Snakemake logs and benchmarks are needed for SOU predictions.
