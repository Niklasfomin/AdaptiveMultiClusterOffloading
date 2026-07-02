#!/usr/bin/env bash
# Download/setup one full CHM13 StainedGlass workflow run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKFLOW_RUNS="${ROOT}/workflow-runs"
TMP_PARENT="${TMPDIR:-/tmp}/snakemake-offloader-large-workflows"
WORKFLOW_NAME="stained-glass"
DEST="${WORKFLOW_RUNS}/stained-glass-chm13-full"
FASTA="${DEST}/resources/chm13v2.0.fa"
FASTA_URL="https://s3-us-west-2.amazonaws.com/human-pangenomics/T2T/CHM13/assemblies/analysis_set/chm13v2.0.fa.gz"
FORCE=0

usage() {
  cat <<'EOF'
Usage: scripts/download-stained-glass-chm13-full.sh [--force]

Creates workflow-runs/stained-glass-chm13-full, downloads CHM13 v2.0
analysis-set FASTA, and builds the FASTA index.

This downloads/unpacks multiple GB of FASTA data.
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
require_cmd curl
require_cmd gunzip
require_cmd samtools

if [[ -e "${DEST}" && "${FORCE}" -eq 0 ]]; then
  echo "[skip] ${DEST} exists (use --force to recreate)"
  exit 0
fi

SOURCE="${WORKFLOW_RUNS}/stained-glass"
if [[ ! -d "${SOURCE}" ]]; then
  clone_or_update "https://github.com/mrvollger/StainedGlass.git" "${WORKFLOW_NAME}"
  SOURCE="${TMP_PARENT}/${WORKFLOW_NAME}"
fi

rm -rf "${DEST}"
mkdir -p "${WORKFLOW_RUNS}"
rsync -a \
  --exclude='.git' \
  --exclude='.snakemake' \
  --exclude='.sou' \
  --exclude='results' \
  --exclude='temp' \
  --exclude='logs' \
  "${SOURCE}/" "${DEST}/"
mkdir -p "${DEST}/logs" "${DEST}/results" "${DEST}/temp" "${DEST}/resources"

echo "[download] CHM13 FASTA"
curl -fL -C - -o "${FASTA}.gz" "${FASTA_URL}"
gunzip -f "${FASTA}.gz"
samtools faidx "${FASTA}"

cat > "${DEST}/config/config.yaml" <<'EOF'
sample: chm13
fasta: resources/chm13v2.0.fa
window: 5000
nbatch: 18
alnthreads: 4
mm_f: 100
tempdir: temp
EOF
awk 'BEGIN{print "fai_entries:"}{printf "  - name: %s\n    length: %s\n",$1,$2}' \
  "${FASTA}.fai" >> "${DEST}/config/config.yaml"

write_sou_config

cat > "${DEST}/README.large.md" <<'EOF'
# Full CHM13 StainedGlass Run

Workflow: `mrvollger/StainedGlass`

Data: CHM13 v2.0 analysis-set FASTA.

This is a large-data workflow and downloads/unpacks multiple GB.
EOF

echo "Created ${DEST}"
