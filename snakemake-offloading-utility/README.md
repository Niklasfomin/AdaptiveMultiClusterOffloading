# Snakemake Offloading Utility (SOU)

SOU is a wrapper around the Snakemake CLI that enables the identification of locally optimal offloading configurations.
It provides a TUI through which prediction parameters and an offloading strategy are set. 
It predicts workflow makespan and execution cost based on historical data from previous runs.

## Installation

Clone this repository and install SOU into the Python environment used for Snakemake:

```bash
pip install <local-path-to-repo>
```

## Usage

In your usual snakemake command, containing all command-line options, replace `snakemake` with `sou`.
This will launch the TUI.

SOU currently expects that the [Snakmake FTP Storage Plugin](https://snakemake.github.io/snakemake-plugin-catalog/plugins/storage/ftp.html) is used for executing workflows.

## Configuration

Prediction parameters and the offloading strategy can be set via the TUI.
Other configuration parameters are set via the `sou-config.yaml` file, expected to be in the current working directory.
See the `sou-config.yaml` file in this repository for an example configuration and the provided JSON schema `sou-config.schema.json`.

## Historical Data

SOU relies on historical data from previous Snakemake runs to make its predictions.
The 'runs directory' provided via the TUI should contain one subdirectory per previous run.
Each subdirectory should contain the workflow log, named 'snakemake.log' and a directory 'benchmarks' containing the benchmark files of the run.
The benchmark files should be named according to the rule they belong to followed by a '+' and their wildcards, e.g. 'rule_name+wildcard1_wildcard2'.