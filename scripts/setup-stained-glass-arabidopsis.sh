#!/usr/bin/env bash
# Prepare the StainedGlass Arabidopsis test config. The workflow
# source tree is already present in workflow-runs/stained-glass (with
# case-example-arabidopsis + the Col-CEN FASTA). This script just
# ensures config/config.yaml is present and editable.
set -euo pipefail

cd "$(dirname "$0")/.."

cd workflow-runs/stained-glass

if [[ ! -f config/config.yaml ]]; then
  echo "config/config.yaml missing in workflow-runs/stained-glass" >&2
  exit 1
fi

# Sanity: confirm the test data is present.
if [[ ! -f resources/Col-CEN_v1.2.fasta ]]; then
  echo "Col-CEN_v1.2.fasta missing under resources/" >&2
  exit 1
fi

echo "StainedGlass Arabidopsis config ready at: $(pwd)/config/config.yaml"
