# Snakemake Offloader
This is a Snakemake executor plugin that enables offloading of Snakemake jobs to an alternative execution environment.
It is intended to be used in conjunction with the Snakemake Offloading Utility (SOU), but can also be used independently.
The current implementation supports Kubernetes and OpenStack Zun as execution environments.

## Installation
First install the Snakemake executor plugins for Kubernetes and OpenStack Zun.
Then clone this repository and install it into the Python environment used for Snakemake:

```bash
pip install <local-path-to-repo>
```

> **Important (editable installs):** Snakemake discovers executor plugins via
> `pkgutil.iter_modules()`, which cannot see modern PEP 660 editable installs.
> If you install this plugin (or the Kubernetes plugin) with `pip install -e`,
> you **must** use legacy compat mode, otherwise `--executor offloader` will
> not be available and Snakemake fails with
> `argument --executor/-e: invalid choice: 'offloader'`:
>
> ```bash
> pip install -e <local-path-to-repo> --config-settings editable_mode=compat
> ```

The OpenStack Zun plugin (`snakemake-executor-plugin-openstack`) is optional.
It is only required if an `openstack:` compute environment is requested.

## Usage
Make Snakemake use this plugin as its executor by adding the following option to your Snakemake command:

```bash
--executor offloader
```

Specify the execution environments like so: `<env-type>:<context>`.

For example:

```bash
--offloader-primary-comp-env kubernetes:cpu04-admin@cpu04-cluster
--offloader-secondary-comp-env kubernetes:cpu15-admin@cpu15-cluster
```

For OpenStack Zun, the context is ignored.

Jobs to be offloaded can be specified via

```bash
--offloader-jobs 1,4,5,6
```

using the JobIDs. However, this parameter is automatically set when using SOU.