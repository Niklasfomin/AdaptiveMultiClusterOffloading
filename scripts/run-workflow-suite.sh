#!/usr/bin/env bash
# Run multiple workflow batches sequentially.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BATCH_SCRIPT="$SCRIPT_DIR/run-workflow-batch.sh"

usage() {
  cat <<EOF
Usage: $(basename "$0") [batch options] [--next batch options ...]

Pass each workflow's normal run-workflow-batch.sh arguments as one group.
Separate workflows with --next. Workflows run sequentially, and the suite
stops immediately if any batch fails.

Example:
  $(basename "$0") \\
    --workdir /path/to/workflow-a --workflow-name workflow-a --runs 5 \\
    --next \\
    --workdir /path/to/workflow-b --workflow-name workflow-b --runs 3
EOF
}

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 2
fi

if [[ $# -eq 1 && ( "$1" == "-h" || "$1" == "--help" ) ]]; then
  usage
  exit 0
fi

run_batch() {
  local index="$1"
  shift

  if [[ $# -eq 0 ]]; then
    echo "Error: workflow ${index} has no arguments." >&2
    exit 2
  fi

  echo ""
  echo "=== Workflow batch ${index} ==="
  bash "$BATCH_SCRIPT" "$@"
}

workflow_index=1
batch_args=()

for arg in "$@"; do
  if [[ "$arg" == "--next" ]]; then
    run_batch "$workflow_index" "${batch_args[@]}"
    batch_args=()
    ((workflow_index += 1))
  else
    batch_args+=("$arg")
  fi
done

run_batch "$workflow_index" "${batch_args[@]}"

echo ""
echo "Workflow suite finished."
