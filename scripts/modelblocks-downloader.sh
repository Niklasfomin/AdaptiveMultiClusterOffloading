#!/usr/bin/env bash
# Download/setup a real-data CO2Stop CDR benchmark using GISCO LAU 2024.
set -euo pipefail

WORKFLOW_RUNS="/srv/nfs/snakemake/niklas/workflow-runs"
TMP_PARENT="/tmp"

WORKFLOW_NAME="module_co2stop_cdr"
DEST="${WORKFLOW_RUNS}/${WORKFLOW_NAME}"
FORCE=0
CONFIGURE_ONLY=0
GISCO_LAU_URL="https://gisco-services.ec.europa.eu/distribution/v2/lau/geojson/LAU_RG_01M_2024_3035.geojson"
CONVERTER_ENV="${TMP_PARENT}/gisco-lau-2024-converter"
CLONED_WORKFLOW_PATH=""
TMP_DIRS=()

cleanup_tmp_dirs() {
  local d
  for d in "${TMP_DIRS[@]:-}"; do
    [[ -n "$d" && -d "$d" ]] || continue
    rm -rf "$d"
  done
}
trap cleanup_tmp_dirs EXIT

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Required command not found: $cmd" >&2
    exit 1
  fi
}

ensure_recreate_allowed() {
  local target="$1"
  local force="$2"

  mkdir -p "$(dirname "$target")"
  if [[ -e "$target" ]]; then
    if ((force)); then
      rm -rf "$target"
    else
      cat >&2 <<EOF
Destination already exists: ${target}
Use --force to recreate it.
EOF
      exit 1
    fi
  fi
}

clone_or_update() {
  local repo_url="$1"
  local name="$2"

  local clone_dir
  clone_dir="$(mktemp -d "${TMP_PARENT}/${name}.XXXXXX")"
  TMP_DIRS+=("$clone_dir")
  git clone --depth 1 "$repo_url" "$clone_dir"
  CLONED_WORKFLOW_PATH="$clone_dir"
}

copy_workflow() {
  local _name="$1"
  local dest="$2"

  if [[ -z "$CLONED_WORKFLOW_PATH" || ! -d "$CLONED_WORKFLOW_PATH" ]]; then
    echo "Internal error: workflow clone path is not set" >&2
    exit 1
  fi

  mkdir -p "$dest"
  rsync -a --delete --exclude ".git" "$CLONED_WORKFLOW_PATH/" "$dest/"
}

usage() {
  cat <<EOF
Usage: $(basename "$0") [--force] [--configure-only]

Creates a CO2Stop CDR benchmark in:
  ${DEST}

The script clones the workflow repo and downloads GISCO LAU 2024 data, then
converts it into the module's per-country GeoParquet input layout.

--force          Recreate the destination directory if it already exists.
--configure-only Only regenerate Snakefile from existing inputs in ${DEST}.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --force)
    FORCE=1
    shift
    ;;
  --configure-only)
    CONFIGURE_ONLY=1
    shift
    ;;
  -h | --help)
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

require_cmd python3
if ((CONFIGURE_ONLY)); then
  if [[ ! -d "${DEST}/resources/inputs" ]]; then
    echo "Expected an already-converted run at ${DEST}" >&2
    exit 1
  fi
else
  require_cmd git
  require_cmd rsync
  require_cmd curl

  ensure_recreate_allowed "${DEST}" "${FORCE}"
  clone_or_update "https://github.com/modelblocks-org/module_co2stop_cdr.git" "${WORKFLOW_NAME}"
  copy_workflow "${WORKFLOW_NAME}" "${DEST}"

  USER_DIR="${DEST}/resources/user"
  SOURCE_FILE="${USER_DIR}/LAU_RG_01M_2024_3035.geojson"
  mkdir -p "${USER_DIR}" "${DEST}/resources/inputs"
  curl --fail --location --retry 3 --continue-at - --output "${SOURCE_FILE}" "${GISCO_LAU_URL}"

  rm -rf "${CONVERTER_ENV}"
  python3 -m venv "${CONVERTER_ENV}"
  "${CONVERTER_ENV}/bin/pip" install --disable-pip-version-check --quiet \
    "geopandas==1.0.1" "pyarrow==17.0.0"

  "${CONVERTER_ENV}/bin/python" - "${SOURCE_FILE}" "${DEST}/resources/inputs" <<'PY'
import sys
from pathlib import Path

import geopandas as gpd

source_file, output_dir = map(Path, sys.argv[1:])
shapes = gpd.read_file(source_file)
shapes = shapes[["GISCO_ID", "CNTR_CODE", "geometry"]].copy()
shapes = shapes.dropna(subset=["GISCO_ID", "CNTR_CODE", "geometry"])
shapes.geometry = shapes.geometry.make_valid()
shapes = shapes.loc[~shapes.geometry.is_empty].copy()
shapes = shapes.to_crs("EPSG:3035")
shapes["shape_id"] = "LAU2024_" + shapes["GISCO_ID"].astype(str)

for country, country_shapes in shapes.groupby("CNTR_CODE", sort=True):
    target = output_dir / f"LAU2024_{country}"
    target.mkdir(parents=True, exist_ok=True)
    country_shapes[["shape_id", "geometry"]].to_parquet(
        target / "shapes.parquet", index=False
    )

print(f"Converted {len(shapes)} polygons into {shapes['CNTR_CODE'].nunique()} country inputs.")
PY
fi

python3 - "${DEST}/Snakefile" "${DEST}/resources/inputs" <<'PY'
import sys
from pathlib import Path

path, inputs_dir = map(Path, sys.argv[1:])
shapes = sorted(directory.name for directory in inputs_dir.iterdir() if directory.is_dir())
if not shapes:
    raise SystemExit("No GISCO LAU country inputs were created")
path.write_text(f'''import yaml
module_config = {{}}
with open("config/config.yaml") as handle:
    module_config["module_co2stop_cdr"] = yaml.safe_load(handle.read())
module module_co2stop_cdr:
    pathvars:
        user_shapes="resources/inputs/{{shapes}}/shapes.parquet",
        cdr_group="results/outputs/{{shapes}}/{{scenario}}/{{cdr_group}}.parquet",
        total_aggregate="results/outputs/{{shapes}}/{{scenario}}/totals.parquet",
        logs="resources/module/logs",
        resources="resources/module/resources",
        results="resources/module/results",
    snakefile: "workflow/Snakefile"
    config: module_config["module_co2stop_cdr"]
use rule * from module_co2stop_cdr as module_co2stop_cdr_*
SHAPES = {shapes!r}
SCENARIOS = ["low", "medium", "high"]
CDR_GROUPS = ["aquifer", "gas", "oil"]
rule all:
    default_target: True
    input:
        expand("results/outputs/{{shapes}}/{{scenario}}/{{cdr_group}}.parquet", shapes=SHAPES, scenario=SCENARIOS, cdr_group=CDR_GROUPS)
''')
PY

cat >"${DEST}/README.large.md" <<'EOF'
# CO2Stop CDR GISCO LAU 2024 Benchmark

Workflow: `modelblocks-org/module_co2stop_cdr`

Dataset: Eurostat GISCO LAU 2024, 1:1 million EPSG:3035 administrative regions.

Scale: 97,987 real polygons, partitioned by country. Each country runs the
three CO2Stop scenarios and three CDR groups, producing over 300 aggregation
targets before supporting workflow jobs.
EOF

echo "Created ${DEST}"
