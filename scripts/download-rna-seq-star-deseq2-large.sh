#!/usr/bin/env bash
# Download/setup one large RNA-seq STAR/DESeq2 workflow run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKFLOW_RUNS="${ROOT}/workflow-runs"
TMP_PARENT="${TMPDIR:-/tmp}/snakemake-offloader-large-workflows"
WORKFLOW_NAME="rna-seq-star-deseq2"
DEST="${WORKFLOW_RUNS}/rna-seq-star-deseq2-large"
FORCE=0

usage() {
  cat <<'EOF'
Usage: scripts/download-rna-seq-star-deseq2-large.sh [--force]

Creates workflow-runs/rna-seq-star-deseq2-large with 48 generated samples
using bundled yeast FASTQs from the upstream workflow repository.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

clone_or_update() {
  local url="$1"
  local name="$2"
  local clone_dest="${TMP_PARENT}/${name}"

  mkdir -p "${TMP_PARENT}"
  if [[ -d "${clone_dest}/.git" ]]; then
    echo "[update] ${name}"
    git -C "${clone_dest}" fetch --depth 1 origin
    git -C "${clone_dest}" reset --hard origin/HEAD
  else
    echo "[clone] ${name}"
    rm -rf "${clone_dest}"
    git clone --depth 1 "${url}" "${clone_dest}"
  fi
}

write_sou_config() {
  cat > "${DEST}/sou-config.yaml" <<'EOF'
clusters:
  primary:
    nodes:
      - name: primary-control-plane
        cores: 48
        mem_gb: 188
        disk_gb: 3000
  secondary:
    nodes:
      - name: secondary-control-plane
        cores: 32
        mem_gb: 125
        disk_gb: 3000
    prices:
      vcpu_hour: 0.04656
      mem_gb_hour: 0.00511
      disk_gb_hour: 0.000132
EOF
}

require_cmd git
require_cmd rsync

if [[ -e "${DEST}" && "${FORCE}" -eq 0 ]]; then
  echo "[skip] ${DEST} exists (use --force to recreate)"
  exit 0
fi

clone_or_update "https://github.com/snakemake-workflows/rna-seq-star-deseq2.git" "${WORKFLOW_NAME}"

rm -rf "${DEST}"
mkdir -p "${WORKFLOW_RUNS}"
rsync -a \
  --exclude='.git' \
  --exclude='.snakemake' \
  --exclude='.sou' \
  --exclude='results' \
  --exclude='temp' \
  --exclude='logs' \
  "${TMP_PARENT}/${WORKFLOW_NAME}/" "${DEST}/"
mkdir -p "${DEST}/logs" "${DEST}/results" "${DEST}/temp"

rm -rf "${DEST}/ngs-test-data"
ln -s ".test/ngs-test-data" "${DEST}/ngs-test-data"

cat > "${DEST}/config/config.yaml" <<'EOF'
samples: config/samples.tsv
units: config/units.tsv

ref:
  species: saccharomyces_cerevisiae
  release: 115
  build: R64-1-1

trimming:
  activate: True

mergeReads:
  activate: False

pca:
  activate: True
  labels:
    - condition

diffexp:
  variables_of_interest:
    condition:
      base_level: untreated
  batch_effects: ""
  contrasts:
    treated-vs-untreated:
      variable_of_interest: condition
      level_of_interest: treated
  model: ~condition

params:
  star:
    index: ""
    align: ""
EOF

{
  printf 'sample_name\tcondition\n'
  for sample_index in $(seq 1 48); do
    if (( sample_index <= 24 )); then
      printf 'T%02d\ttreated\n' "${sample_index}"
    else
      printf 'U%02d\tuntreated\n' "${sample_index}"
    fi
  done
} > "${DEST}/config/samples.tsv"

{
  printf 'sample_name\tunit_name\tfq1\tfq2\tsra\tfastp_adapters\tfastp_extra\tstrandedness\n'
  for sample_index in $(seq 1 48); do
    if (( sample_index <= 24 )); then
      sample_name="$(printf 'T%02d' "${sample_index}")"
    else
      sample_name="$(printf 'U%02d' "${sample_index}")"
    fi
    case $(( sample_index % 3 )) in
      0) fastq_name=a; strandedness=yes ;;
      1) fastq_name=b; strandedness=reverse ;;
      2) fastq_name=c; strandedness=none ;;
    esac
    printf '%s\t1\tngs-test-data/reads/%s.scerevisiae.1.fq\tngs-test-data/reads/%s.scerevisiae.2.fq\t\t--adapter_sequence=AGATCGGAAGAGCACACGTCTGAACTCCAGTCA\t\t%s\n' \
      "${sample_name}" "${fastq_name}" "${fastq_name}" "${strandedness}"
  done
} > "${DEST}/config/units.tsv"

write_sou_config

cat > "${DEST}/README.large.md" <<'EOF'
# Large RNA-seq STAR/DESeq2 Run

Catalog workflow: `snakemake-workflows/rna-seq-star-deseq2`

Scale: 48 generated samples using bundled yeast test FASTQs.

Validated dry-run size: 493 jobs.
EOF

echo "Created ${DEST}"
