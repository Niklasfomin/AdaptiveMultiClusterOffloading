#!/usr/bin/env bash
# Ensure the Arabidopsis test data is in place. The workflow ships
# resources/Col-CEN_v1.2.fasta + its .fai index in the repo. This
# script is a no-op when the files are already present; it would
# otherwise download from the canonical mirror.
set -euo pipefail

cd "$(dirname "$0")/.."

cd workflow-runs/stained-glass

fasta="resources/Col-CEN_v1.2.fasta"
fai="${fasta}.fai"

if [[ -f "${fasta}" && -f "${fai}" ]]; then
  echo "[skip] ${fasta} and ${fai} already present"
  exit 0
fi

mkdir -p resources

if [[ ! -f "${fasta}" ]]; then
  echo "Downloading Col-CEN_v1.2.fasta ..."
  curl -fL --retry 3 -o "${fasta}.tmp" \
    "https://github.com/snakemake/snakemake-workflow-tutorial/raw/main/resources/Col-CEN_v1.2.fasta" \
    || {
      echo "Pre-bundled fasta missing and canonical download failed." >&2
      echo "Place Col-CEN_v1.2.fasta under workflow-runs/stained-glass/resources/ first." >&2
      exit 1
    }
  mv "${fasta}.tmp" "${fasta}"
fi

if [[ ! -f "${fai}" ]]; then
  echo "Building ${fai} ..."
  python3 -c "import pysam; pysam.faidx('${fasta}')"
fi

echo "Arabidopsis test data ready under: $(pwd)/resources"
