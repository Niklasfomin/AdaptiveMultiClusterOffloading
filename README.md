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
