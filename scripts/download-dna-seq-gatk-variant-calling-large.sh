#!/usr/bin/env bash
# Download/setup one large DNA-seq GATK variant-calling workflow run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKFLOW_RUNS="${ROOT}/workflow-runs"
TMP_PARENT="${TMPDIR:-/tmp}/snakemake-offloader-large-workflows"
WORKFLOW_NAME="dna-seq-gatk-variant-calling"
DEST="${WORKFLOW_RUNS}/dna-seq-gatk-variant-calling-large"
FORCE=0

usage() {
  cat <<'EOF'
Usage: scripts/download-dna-seq-gatk-variant-calling-large.sh [--force]

Creates workflow-runs/dna-seq-gatk-variant-calling-large with 48 generated
samples / 72 generated units using bundled yeast FASTQs from the upstream
workflow repository.
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

clone_or_update "https://github.com/snakemake-workflows/dna-seq-gatk-variant-calling.git" "${WORKFLOW_NAME}"

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

rm -rf "${DEST}/data"
ln -s ".test/data" "${DEST}/data"
cp "${DEST}/.test/config/config.yaml" "${DEST}/config/config.yaml"

{
  printf 'sample\n'
  for sample_index in $(seq 1 48); do
    printf 'S%02d\n' "${sample_index}"
  done
} > "${DEST}/config/samples.tsv"

{
  printf 'sample\tunit\tplatform\tfq1\tfq2\n'
  for sample_index in $(seq 1 48); do
    sample_name="$(printf 'S%02d' "${sample_index}")"
    printf '%s\t1\tILLUMINA\tdata/reads.1.fq.gz\tdata/reads.2.fq.gz\n' "${sample_name}"
    if (( sample_index % 2 == 0 )); then
      printf '%s\t2\tILLUMINA\tdata/reads.1.fq.gz\t\n' "${sample_name}"
    fi
  done
} > "${DEST}/config/units.tsv"

write_sou_config

cat > "${DEST}/README.large.md" <<'EOF'
# Large DNA-seq GATK Variant-Calling Run

Catalog workflow: `snakemake-workflows/dna-seq-gatk-variant-calling`

Scale: 48 generated samples / 72 generated units using bundled yeast test FASTQs.

Validated dry-run size: 452 jobs.
EOF

echo "Created ${DEST}"
