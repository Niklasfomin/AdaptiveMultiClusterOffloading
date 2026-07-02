#!/usr/bin/env bash
# Setup/download a larger human WXS GATK variant-calling workflow run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKFLOW_RUNS="${ROOT}/workflow-runs"
TMP_PARENT="${TMPDIR:-/tmp}/snakemake-offloader-large-workflows"
WORKFLOW_NAME="dna-seq-gatk-variant-calling"
DEST="${WORKFLOW_RUNS}/dna-seq-gatk-wxs-prjeb14677"
STUDY="PRJEB14677"
TARGET_GB="180"
FORCE=0
DOWNLOAD=1

usage() {
  cat <<'EOF'
Usage: scripts/download-dna-seq-gatk-wxs-prjeb14677.sh [OPTIONS]

Creates workflow-runs/dna-seq-gatk-wxs-prjeb14677 with a human WXS subset
from ENA study PRJEB14677 for the snakemake-workflows/dna-seq-gatk-variant-calling
workflow.

Options:
  --target-gb GB   Select paired FASTQ runs until compressed input reaches GB.
                   Default: 180
  --no-download   Create workflow/config/download.urls only; do not download FASTQs.
  --force         Recreate the destination directory.
  -h, --help      Show this help.

Recommended: run this on the NFS server node, not on the Mac, because the
selected dataset is intentionally >100GB.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-gb)
      TARGET_GB="${2:?Missing value for --target-gb}"
      shift 2
      ;;
    --no-download)
      DOWNLOAD=0
      shift
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
require_cmd curl
require_cmd python3

if [[ -e "${DEST}" && "${FORCE}" -eq 0 ]]; then
  echo "[skip] ${DEST} exists (use --force to recreate)"
  exit 0
fi

if [[ -e "${DEST}" ]]; then
  rm -rf "${DEST}"
fi

clone_or_update "https://github.com/snakemake-workflows/dna-seq-gatk-variant-calling.git" "${WORKFLOW_NAME}"

mkdir -p "${WORKFLOW_RUNS}"
rsync -a \
  --exclude='.git' \
  --exclude='.snakemake' \
  --exclude='.sou' \
  --exclude='results' \
  --exclude='temp' \
  --exclude='logs' \
  "${TMP_PARENT}/${WORKFLOW_NAME}/" "${DEST}/"
mkdir -p "${DEST}/logs" "${DEST}/results" "${DEST}/temp" "${DEST}/data/fastq"

cp "${DEST}/.test/config/config.yaml" "${DEST}/config/config.yaml"
python3 - "${DEST}/config/config.yaml" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
text = text.replace("species: saccharomyces_cerevisiae", "species: homo_sapiens")
text = text.replace("release: 98", "release: 115")
text = text.replace("build: R64-1-1", "build: GRCh38")
path.write_text(text)
PY

metadata_url="https://www.ebi.ac.uk/ena/portal/api/filereport?accession=${STUDY}&result=read_run&fields=run_accession,fastq_ftp,fastq_bytes,sample_accession&format=tsv&limit=0"
echo "[meta] downloading ENA metadata for ${STUDY}"
curl -L "${metadata_url}" -o "${DEST}/${STUDY}.ena_runs.tsv"

echo "[select] selecting paired WXS runs up to ${TARGET_GB}GB compressed"
python3 - "${DEST}/${STUDY}.ena_runs.tsv" "${TARGET_GB}" "${DEST}" <<'PY'
import csv
import sys
from pathlib import Path

metadata, target_gb, dest = sys.argv[1:]
target_bytes = float(target_gb) * 1e9
dest = Path(dest)

selected = []
total_bytes = 0

with open(metadata, newline="") as handle:
    reader = csv.DictReader(handle, delimiter="\t")
    for row in reader:
        urls = row["fastq_ftp"].split(";") if row.get("fastq_ftp") else []
        sizes = [int(value) for value in row["fastq_bytes"].split(";") if value]
        if len(urls) < 2 or len(sizes) < 2:
            continue
        if len(urls) == 3 and len(sizes) == 3:
            urls = urls[1:]
            sizes = sizes[1:]
        if len(urls) != 2 or len(sizes) != 2:
            continue
        selected.append((row["run_accession"], urls, sizes))
        total_bytes += sum(sizes)
        if total_bytes >= target_bytes:
            break

if not selected:
    raise SystemExit("No paired FASTQ runs selected")

with open(dest / "config" / "samples.tsv", "w", newline="") as handle:
    writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
    writer.writerow(["sample"])
    for index, _entry in enumerate(selected, 1):
        writer.writerow([f"W{index:04d}"])

with open(dest / "config" / "units.tsv", "w", newline="") as handle:
    writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
    writer.writerow(["sample", "unit", "platform", "fq1", "fq2"])
    for index, (run, _urls, _sizes) in enumerate(selected, 1):
        sample = f"W{index:04d}"
        writer.writerow([
            sample,
            "1",
            "ILLUMINA",
            f"data/fastq/{run}_1.fastq.gz",
            f"data/fastq/{run}_2.fastq.gz",
        ])

with open(dest / "download.urls", "w") as handle:
    for run, urls, _sizes in selected:
        for mate, url in enumerate(urls, 1):
            filename = f"data/fastq/{run}_{mate}.fastq.gz"
            handle.write(f"https://{url}\n")

with open(dest / "download.manifest.tsv", "w", newline="") as handle:
    writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
    writer.writerow(["sample", "run_accession", "fq1", "fq2", "compressed_bytes"])
    for index, (run, _urls, sizes) in enumerate(selected, 1):
        writer.writerow([
            f"W{index:04d}",
            run,
            f"data/fastq/{run}_1.fastq.gz",
            f"data/fastq/{run}_2.fastq.gz",
            sum(sizes),
        ])

print(f"selected_runs={len(selected)}")
print(f"compressed_GB={total_bytes / 1e9:.2f}")
PY

write_sou_config

cat > "${DEST}/README.large.md" <<EOF
# Human WXS GATK Variant-Calling Run

Catalog workflow: \`snakemake-workflows/dna-seq-gatk-variant-calling\`

Input data: ENA study \`${STUDY}\`, human paired-end WXS FASTQs.

This run is generated by:

\`\`\`bash
scripts/download-dna-seq-gatk-wxs-prjeb14677.sh --target-gb ${TARGET_GB}
\`\`\`

Generated files:

- \`config/samples.tsv\`
- \`config/units.tsv\`
- \`download.urls\`
- \`download.manifest.tsv\`
- \`${STUDY}.ena_runs.tsv\`

The workflow config uses \`homo_sapiens\`, Ensembl release \`115\`, build
\`GRCh38\`.
EOF

if [[ "${DOWNLOAD}" -eq 1 ]]; then
  echo "[download] downloading selected FASTQs into ${DEST}/data/fastq"
  if command -v aria2c >/dev/null 2>&1; then
    aria2c \
      --continue=true \
      --max-concurrent-downloads=8 \
      --split=4 \
      --max-connection-per-server=4 \
      --dir="${DEST}/data/fastq" \
      --input-file="${DEST}/download.urls"
  elif command -v wget >/dev/null 2>&1; then
    wget --continue --directory-prefix="${DEST}/data/fastq" --input-file="${DEST}/download.urls"
  else
    while IFS= read -r url; do
      filename="${url##*/}"
      echo "[download] ${filename}"
      curl -L --continue-at - --output "${DEST}/data/fastq/${filename}" "${url}"
    done < "${DEST}/download.urls"
  fi
  du -sh "${DEST}/data/fastq"
else
  echo "[skip] download disabled. URLs written to ${DEST}/download.urls"
fi

echo "[done] ${DEST}"
