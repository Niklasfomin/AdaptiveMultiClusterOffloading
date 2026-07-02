#!/usr/bin/env bash
# Create .venv in repo root and install the executor plugin wheels
# plus snakemake and the offloader workflow tools.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade --quiet pip wheel

python -m pip install --quiet \
  ./snakemake-executor-plugin-kubernetes \
  ./snakemake-executor-plugin-openstack \
  ./snakemake-offloader \
  ./snakemake-offloading-utility

python -m pip install --quiet \
  "snakemake==9.23.0" \
  "snakemake-storage-plugin-ftp" \
  "kubernetes>=27.2.0,<31"

python -c "import snakemake; print('snakemake', snakemake.__version__)"
python -c "import snakemake_executor_plugin_offloader; print('offloader OK')"
python -c "import snakemake_executor_plugin_kubernetes; print('k8s OK')"
