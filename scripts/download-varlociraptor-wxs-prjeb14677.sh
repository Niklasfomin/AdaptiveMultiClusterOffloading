#!/usr/bin/env bash
# Setup a Varlociraptor workflow run with real human WXS data from ENA PRJEB14677.
# Uses BWA (low memory ~4GB/job) with scatter/gather for massive DAG fanout.
# Reuses FASTQs already downloaded by download-dna-seq-gatk-wxs-prjeb14677.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKFLOW_RUNS="${ROOT}/workflow-runs"
TMP_PARENT="${TMPDIR:-/tmp}/snakemake-offloader-large-workflows"
WORKFLOW_NAME="dna-seq-varlociraptor"
DEST="${WORKFLOW_RUNS}/dna-seq-varlociraptor-wxs-prjeb14677"
GATK_WXS_DEST="${WORKFLOW_RUNS}/dna-seq-gatk-wxs-prjeb14677"
STUDY="PRJEB14677"
FORCE=0
N_GROUPS="${N_GROUPS:-66}"

usage() {
  cat <<'EOF'
Usage: scripts/download-varlociraptor-wxs-prjeb14677.sh [OPTIONS]

Creates workflow-runs/dna-seq-varlociraptor-wxs-prjeb14677 with real human WXS
FASTQs from ENA study PRJEB14677 for the snakemake-workflows/dna-seq-varlociraptor
workflow.

Requires that download-dna-seq-gatk-wxs-prjeb14677.sh has already been run
(FASTQs are symlinked from workflow-runs/dna-seq-gatk-wxs-prjeb14677/data/fastq/).

Options:
  --n-groups N     Number of tumor/normal groups to create. Default: 66
  --force          Recreate the destination directory.
  -h, --help       Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --n-groups)
      N_GROUPS="${2:?Missing value for --n-groups}"
      shift 2
      ;;
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
      - name: siena06-primary-control-plane
        cores: 48
        mem_gb: 188
        disk_gb: 2910
  secondary:
    nodes:
      - name: cpu06-secondary-control-plane
        cores: 32
        mem_gb: 125
        disk_gb: 3313
    prices:
      vcpu_hour: 0.04656
      mem_gb_hour: 0.00511
      disk_gb_hour: 0.000132
EOF
}

require_cmd git
require_cmd rsync
require_cmd python3

if [[ ! -d "${GATK_WXS_DEST}/data/fastq" ]]; then
  echo "ERROR: ${GATK_WXS_DEST}/data/fastq not found." >&2
  echo "Run scripts/download-dna-seq-gatk-wxs-prjeb14677.sh first." >&2
  exit 1
fi

if [[ -e "${DEST}" && "${FORCE}" -eq 0 ]]; then
  echo "[skip] ${DEST} exists (use --force to recreate)"
  exit 0
fi

if [[ -e "${DEST}" ]]; then
  rm -rf "${DEST}"
fi

clone_or_update "https://github.com/snakemake-workflows/dna-seq-varlociraptor.git" "${WORKFLOW_NAME}"

mkdir -p "${WORKFLOW_RUNS}"
rsync -a \
  --exclude='.git' \
  --exclude='.snakemake' \
  --exclude='.sou' \
  --exclude='results' \
  --exclude='temp' \
  --exclude='logs' \
  "${TMP_PARENT}/${WORKFLOW_NAME}/" "${DEST}/"
mkdir -p "${DEST}/logs" "${DEST}/results" "${DEST}/temp" "${DEST}/data"

# Symlink the FASTQs from the GATK WXS download
ln -s "../dna-seq-gatk-wxs-prjeb14677/data/fastq" "${DEST}/data/fastq"
echo "[link] ${DEST}/data/fastq -> ${GATK_WXS_DEST}/data/fastq"

# Read the GATK WXS manifest to get run accessions
MANIFEST="${GATK_WXS_DEST}/download.manifest.tsv"
if [[ ! -f "${MANIFEST}" ]]; then
  echo "ERROR: ${MANIFEST} not found." >&2
  exit 1
fi

# Generate tumor/normal pairs from the WXS runs
python3 - "${MANIFEST}" "${DEST}" "${N_GROUPS}" <<'PY'
import csv
import os
import sys
from pathlib import Path

manifest, dest, n_groups = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])

runs = []
with open(manifest, newline="") as handle:
    reader = csv.DictReader(handle, delimiter="\t")
    for row in reader:
        runs.append((row["sample"], row["run_accession"], row["fq1"], row["fq2"]))

if len(runs) < n_groups * 2:
    print(f"WARNING: only {len(runs)} runs available, need {n_groups*2} for {n_groups} groups")
    n_groups = len(runs) // 2

groups = min(n_groups, len(runs) // 2)

with open(dest / "config" / "samples.tsv", "w", newline="") as f:
    writer = csv.writer(f, delimiter="\t", lineterminator="\n")
    f.write("sample_name\tgroup\talias\tplatform\tdatatype\tcalling\n")
    for g in range(1, groups + 1):
        normal_sample, normal_run, _, _ = runs[(g - 1) * 2]
        tumor_sample, tumor_run, _, _ = runs[(g - 1) * 2 + 1]
        writer.writerow([f"N{g:03d}", f"g{g:03d}", "x", "ILLUMINA", "dna", "variants"])
        writer.writerow([f"T{g:03d}", f"g{g:03d}", "y", "ILLUMINA", "dna", "variants"])

with open(dest / "config" / "units.tsv", "w", newline="") as f:
    writer = csv.writer(f, delimiter="\t", lineterminator="\n")
    f.write("sample_name\tunit_name\tgroup\tfq1\tfq2\tadapters\tsra\n")
    for g in range(1, groups + 1):
        normal_sample, normal_run, normal_fq1, normal_fq2 = runs[(g - 1) * 2]
        tumor_sample, tumor_run, tumor_fq1, tumor_fq2 = runs[(g - 1) * 2 + 1]
        writer.writerow([f"N{g:03d}", "lane1", f"g{g:03d}", f"data/fastq/{normal_run}_1.fastq.gz", f"data/fastq/{normal_run}_2.fastq.gz", "", ""])
        writer.writerow([f"T{g:03d}", "lane1", f"g{g:03d}", f"data/fastq/{tumor_run}_1.fastq.gz", f"data/fastq/{tumor_run}_2.fastq.gz", "", ""])

print(f"groups={groups} samples={groups*2} fastq_pairs={groups*2}")
PY

# Configure for human WXS
python3 - "${DEST}/config/config.yaml" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
text = text.replace("n_chromosomes: 17", "n_chromosomes: 25")
text = text.replace("species: saccharomyces_cerevisiae", "species: homo_sapiens")
text = text.replace("release: 100", "release: 111")
text = text.replace("build: R64-1-1", "build: GRCh38")
path.write_text(text)
PY

# Use the simple per-group scenario (tumor/normal)
cat > "${DEST}/config/scenario.yaml" <<'EOF'
samples:
  x:
    resolution: 0.1
    universe: "[0.0,1.0]"
  y:
    resolution: 0.1
    universe: "[0.0,1.0]"

events:
  x: "x:]0.0,1.0] & y:0.0"
  y: "y:]0.0,1.0] & x:0.0"
  both: "y:]0.0,1.0] & x:]0.0,1.0]"
EOF

write_sou_config

cat > "${DEST}/README.large.md" <<EOF
# Varlociraptor Human WXS Run

Catalog workflow: \`snakemake-workflows/dna-seq-varlociraptor\`

Input data: ENA study \`${STUDY}\`, human paired-end WXS FASTQs.
FASTQs are symlinked from \`workflow-runs/dna-seq-gatk-wxs-prjeb14677/data/fastq/\`.

Reference: homo_sapiens, Ensembl release 111, build GRCh38, 25 chromosomes.

Memory profile (per job):
- BWA alignment: ~4GB
- Freebayes (scattered): ~2-4GB per chunk
- Varlociraptor: ~1-2GB per chunk
- GATK BaseRecalibrator: ~8GB

Safe to run with --jobs 8 (~32GB concurrent).

Run \`${N_GROUPS}\` tumor/normal groups (\`${N_GROUPS}\*2\) samples).
EOF

echo "[done] ${DEST}"
echo "Run snakemake -n --cores 1 to check job count."
