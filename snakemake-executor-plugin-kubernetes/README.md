# Snakemake Executor Plugin for Kubernetes (Offloader Version)

This is an adapted version of the [Snakemake executor plugin for Kubernetes](https://github.com/snakemake/snakemake-executor-plugin-kubernetes). 

It is intended to be used in conjunction with the Snakemake Offloader. However, it can also be used independently.

This version has been modified in the following ways:
- Added cluster resource utilization logging
- Improved logging to differentiate between time of job submission and container creation
- Added support for simplified GCP authentication
- Allow specification of kube context
- Fix completed jobs deletion


## Installation
Clone this repository and install it into the Python environment used for Snakemake:

```bash
pip install <local-path-to-repo>
```


## Usage

The same parameters as the original plugin are used, documented [here](https://snakemake.github.io/snakemake-plugin-catalog/plugins/executor/kubernetes.html).

In addition, the following parameters are supported:
- `--kubernetes-log-resource-utilization` turns on logging of cluster resource utilization when jobs are submitted or completed.
- `--kubernetes-gcp-app-creds-file <path_to_file>` can be used to specify the path to a GCP service account key file. 
This is a convenience function for providing access to google cloud stroage, especially when using the GCP storage provider plugin.
It avoids the need of setting up identity workload federation in the Kubernetes cluster.