# Snakemake Executor Plugin for OpenStack Zun

This is a Snakemake Executor Plugin for OpenStack Zun. 

It is intended to be used in conjunction with the Snakemake Offloader. However, it can also be used independently.

## Installation
Clone this repository and install it into the Python environment used for Snakemake:

```bash
pip install <local-path-to-repo>
```

## Usage
To authenticate with your OpenStack cloud, source the OpenStack RC file:
```source admin-openrc.sh```

Use `--openstack-default-volume-size <size-in-GB>` to specify the size of the Cinder volume that will be attached to each container.

When using the GCP storage provider plugin, you can specify the path to a GCP service account key file like so:

```export GCS_SERVICE_ACCOUNT_KEY=$(<path-to-key-file>)```

This avoids the need of setting up identity workload federation in OpenStack.
