#!/usr/bin/env bash
# Clone (or update) the workflow repositories into workflow-runs/<name>.
# This repo already ships a checked-out copy of stained-glass in
# workflow-runs/stained-glass; we keep this script for symmetry with the
# handoff plan.
set -euo pipefail

cd "$(dirname "$0")/.."

mkdir -p workflow-runs

declare -A REPOS=(
  [stained-glass]="https://github.com/mrvollger/StainedGlass.git"
)

for name in "${!REPOS[@]}"; do
  url="${REPOS[$name]}"
  target="workflow-runs/${name}"
  if [[ -d "${target}/.git" ]]; then
    echo "[skip] ${name} already cloned at ${target}"
    continue
  fi
  echo "[clone] ${name} -> ${target}"
  git clone --depth 1 "${url}" "${target}"
done
