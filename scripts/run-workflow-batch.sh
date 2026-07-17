#!/usr/bin/env bash
# Run a Snakemake workflow repeatedly and archive each run in experimental-data format.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

WORKDIR="/srv/nfs/snakemake/niklas/workflow-runs/stained-glass"
WORKFLOW_NAME=""
RUNS=30
JOBS=100
CORES=""
MAX_THREADS=12

SNAKEFILE="workflow/Snakefile"
CONFIGFILE="config/config.yaml"
EXECUTOR="offloader"
CONTAINER_IMAGE="snakemake-stainedglass:v9.23.0-linux"
PRIMARY_ENV="kubernetes:cluster1"
SECONDARY_ENV=""
NAMESPACE=""
PERSISTENT_VOLUMES="snakemake-nfs-pvc:/srv/nfs/snakemake"
LATENCY_WAIT=120
BENCHMARK_EXTENDED=1
SHARED_FS_USAGE=(input-output sources source-cache storage-local-copies)

ARCHIVE_SECTION="experiments/no_offloading"
ARCHIVE_BASE=""
RUN_SUFFIX="no_offl"
PROFILE_CONFIG=""
PROFILE_NAME=""
CONTINUE_ON_ERROR=0

CLEANUP_DIRS=(results temp logs)
EXTRA_ARGS=()
COPY_SPECS=()

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

General:
  --workdir PATH              Workflow run directory (default: ${WORKDIR})
  --workflow-name NAME        Workflow name under experimental-data/ (default: basename of workdir)
  --runs N                    Number of runs (default: ${RUNS})

Snakemake runtime:
  --snakefile PATH            Snakefile path relative to workdir (default: ${SNAKEFILE})
  --configfile PATH           Config path relative to workdir (default: ${CONFIGFILE})
  --jobs N                    Snakemake --jobs value (default: ${JOBS})
  --cores N                   Snakemake --cores value (default: unset)
  --max-threads N             Snakemake --max-threads value (default: ${MAX_THREADS})
  --executor NAME             Snakemake executor (default: ${EXECUTOR})
  --container-image IMAGE     Container image (default: ${CONTAINER_IMAGE})
  --primary-env ENV           --offloader-primary-comp-env (default: ${PRIMARY_ENV})
  --secondary-env ENV         --offloader-secondary-comp-env (default: disabled)
  --namespace NAME            --offloader-namespace (default: plugin default)
  --persistent-volumes SPEC   --offloader-persistent-volumes (default: ${PERSISTENT_VOLUMES})
  --latency-wait N            Snakemake --latency-wait (default: ${LATENCY_WAIT})
  --no-benchmark-extended     Do not pass --benchmark-extended

Archiving:
  --archive-section PATH      Path under workflow in experimental-data (default: ${ARCHIVE_SECTION})
  --archive-base PATH         Full archive base path (overrides workflow+section)
  --run-suffix NAME           Run folder suffix (default: ${RUN_SUFFIX})
  --profile-config PATH       Relative path in workdir to profile config to copy
  --profile-name NAME         Target folder name for profile config in archive run dir
  --copy-file SRC:DST         Extra copy spec (SRC relative to workdir, DST relative to run dir), repeatable

Cleanup:
  --cleanup-dir NAME          Add dir to cleanup list (repeatable). Default: results,temp,logs
  --no-default-cleanup        Start cleanup list empty

Control:
  --extra-arg ARG             Extra argument forwarded to snakemake (repeatable)
  --snakemake-arg ARG         Alias for --extra-arg (repeatable)
  --                          Pass all remaining args directly to snakemake
  --continue-on-error         Continue batch even if one run fails
  -h, --help                  Show help

Output per run:
  <archive-base>/run_<YYYYMMDD>_<HHMMSS>_<run-suffix>/
    benchmarks/
    snakemake.log
    config/config.yaml
    <profile-name>/config.yaml   (if --profile-config used)
    run-metadata.txt
EOF
}

resolve_in_workdir() {
  local p="$1"
  if [[ "$p" = /* ]]; then
    printf '%s' "$p"
  else
    printf '%s' "$WORKDIR/$p"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workdir) WORKDIR="$2"; shift 2 ;;
    --workflow-name) WORKFLOW_NAME="$2"; shift 2 ;;
    --runs) RUNS="$2"; shift 2 ;;

    --snakefile) SNAKEFILE="$2"; shift 2 ;;
    --configfile) CONFIGFILE="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --max-threads) MAX_THREADS="$2"; shift 2 ;;
    --executor) EXECUTOR="$2"; shift 2 ;;
    --container-image) CONTAINER_IMAGE="$2"; shift 2 ;;
    --primary-env) PRIMARY_ENV="$2"; shift 2 ;;
    --secondary-env) SECONDARY_ENV="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --persistent-volumes) PERSISTENT_VOLUMES="$2"; shift 2 ;;
    --latency-wait) LATENCY_WAIT="$2"; shift 2 ;;
    --no-benchmark-extended) BENCHMARK_EXTENDED=0; shift ;;

    --archive-section) ARCHIVE_SECTION="$2"; shift 2 ;;
    --archive-base) ARCHIVE_BASE="$2"; shift 2 ;;
    --run-suffix) RUN_SUFFIX="$2"; shift 2 ;;
    --profile-config) PROFILE_CONFIG="$2"; shift 2 ;;
    --profile-name) PROFILE_NAME="$2"; shift 2 ;;
    --copy-file) COPY_SPECS+=("$2"); shift 2 ;;

    --cleanup-dir) CLEANUP_DIRS+=("$2"); shift 2 ;;
    --no-default-cleanup) CLEANUP_DIRS=(); shift ;;

    --extra-arg) EXTRA_ARGS+=("$2"); shift 2 ;;
    --snakemake-arg) EXTRA_ARGS+=("$2"); shift 2 ;;
    --) shift; EXTRA_ARGS+=("$@"); break ;;
    --continue-on-error) CONTINUE_ON_ERROR=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ ! -d "$WORKDIR" ]]; then
  echo "Error: workdir does not exist: $WORKDIR" >&2
  exit 1
fi

if [[ -z "$WORKFLOW_NAME" ]]; then
  WORKFLOW_NAME="$(basename "$WORKDIR")"
fi

# Normalize common mistaken section for training runs.
if [[ "$ARCHIVE_SECTION" == "experiments/training" || "$ARCHIVE_SECTION" == "experiments/training/" ]]; then
  echo "Info: normalizing --archive-section ${ARCHIVE_SECTION} to training" >&2
  ARCHIVE_SECTION="training"
fi

if [[ -z "$ARCHIVE_BASE" ]]; then
  ARCHIVE_BASE="${ROOT}/experimental-data/${WORKFLOW_NAME}/${ARCHIVE_SECTION}"
fi

if [[ -n "$PROFILE_CONFIG" && -z "$PROFILE_NAME" ]]; then
  PROFILE_NAME="$(basename "$(dirname "$PROFILE_CONFIG")")"
fi

mkdir -p "$ARCHIVE_BASE"

cleanup_runtime_dirs() {
  local d
  for d in "${CLEANUP_DIRS[@]}"; do
    [[ -n "$d" ]] || continue
    rm -rf "$WORKDIR/$d"
  done
}

prepare_runtime_dirs() {
  local d
  for d in "${CLEANUP_DIRS[@]}"; do
    [[ -n "$d" ]] || continue
    mkdir -p "$WORKDIR/$d"
  done
}

move_without_preserve() {
  local src="$1"
  local dst="$2"

  if [[ -d "$src" ]]; then
    mkdir -p "$dst"
    cp -R --no-preserve=all "$src"/. "$dst"/
    rm -rf "$src"
  elif [[ -f "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp --no-preserve=all "$src" "$dst"
    rm -f "$src"
  fi
}

archive_run() {
  local run_index="$1"
  local status="$2"
  local started_at="$3"
  local finished_at="$4"

  local ts run_name archive_dir
  ts="$(date +%Y%m%d_%H%M%S)"
  run_name="run_${ts}_${RUN_SUFFIX}"
  archive_dir="${ARCHIVE_BASE}/${run_name}"

  while [[ -e "$archive_dir" ]]; do
    sleep 1
    ts="$(date +%Y%m%d_%H%M%S)"
    run_name="run_${ts}_${RUN_SUFFIX}"
    archive_dir="${ARCHIVE_BASE}/${run_name}"
  done

  mkdir -p "$archive_dir"
  mkdir -p "$archive_dir/config"

  if [[ -d "$WORKDIR/benchmarks" ]]; then
    move_without_preserve "$WORKDIR/benchmarks" "$archive_dir/benchmarks"
  else
    mkdir -p "$archive_dir/benchmarks"
  fi

  if [[ -f "$WORKDIR/snakemake.log" ]]; then
    move_without_preserve "$WORKDIR/snakemake.log" "$archive_dir/snakemake.log"
  fi

  local cfg_abs
  cfg_abs="$(resolve_in_workdir "$CONFIGFILE")"
  if [[ -f "$cfg_abs" ]]; then
    cp "$cfg_abs" "$archive_dir/config/config.yaml"
  fi

  if [[ -n "$PROFILE_CONFIG" ]]; then
    local prof_abs
    prof_abs="$(resolve_in_workdir "$PROFILE_CONFIG")"
    if [[ -f "$prof_abs" ]]; then
      local prof_target_name
      prof_target_name="${PROFILE_NAME:-profile}"
      mkdir -p "$archive_dir/${prof_target_name}"
      cp "$prof_abs" "$archive_dir/${prof_target_name}/config.yaml"
    fi
  fi

  local spec
  for spec in "${COPY_SPECS[@]}"; do
    local src dst src_abs dst_abs dst_dir
    if [[ "$spec" != *:* ]]; then
      echo "Warning: ignoring invalid --copy-file spec (expected SRC:DST): $spec" >&2
      continue
    fi
    src="${spec%%:*}"
    dst="${spec#*:}"
    src_abs="$(resolve_in_workdir "$src")"
    dst_abs="$archive_dir/$dst"
    dst_dir="$(dirname "$dst_abs")"
    if [[ -f "$src_abs" ]]; then
      mkdir -p "$dst_dir"
      cp "$src_abs" "$dst_abs"
    fi
  done

  cat > "$archive_dir/run-metadata.txt" <<META
run_index=${run_index}
status=${status}
started_at=${started_at}
finished_at=${finished_at}
workdir=${WORKDIR}
workflow_name=${WORKFLOW_NAME}
archive_base=${ARCHIVE_BASE}
archive_section=${ARCHIVE_SECTION}
runs_total=${RUNS}
jobs=${JOBS}
cores=${CORES}
max_threads=${MAX_THREADS}
snakefile=${SNAKEFILE}
configfile=${CONFIGFILE}
executor=${EXECUTOR}
container_image=${CONTAINER_IMAGE}
primary_env=${PRIMARY_ENV}
secondary_env=${SECONDARY_ENV}
persistent_volumes=${PERSISTENT_VOLUMES}
namespace=${NAMESPACE}
latency_wait=${LATENCY_WAIT}
META

  echo "Archived run #${run_index} to: ${archive_dir}"
}

run_one() {
  local i="$1"

  cleanup_runtime_dirs
  prepare_runtime_dirs
  rm -rf "$WORKDIR/benchmarks"
  rm -f "$WORKDIR/snakemake.log"

  local started_at
  started_at="$(date --iso-8601=seconds)"

  local cmd=(
    snakemake
    --snakefile "$SNAKEFILE"
    --configfile "$CONFIGFILE"
    --jobs "$JOBS"
    --max-threads "$MAX_THREADS"
    --show-failed-logs
    --printshellcmds
    --latency-wait "$LATENCY_WAIT"
    --rerun-incomplete
    --nolock
  )

  if [[ -n "$CORES" ]]; then
    cmd+=(--cores "$CORES")
  fi

  if [[ "$BENCHMARK_EXTENDED" -eq 1 ]]; then
    cmd+=(--benchmark-extended)
  fi

  if [[ -n "$EXECUTOR" ]]; then
    cmd+=(--executor "$EXECUTOR")
  fi
  if [[ -n "$CONTAINER_IMAGE" ]]; then
    cmd+=(--container-image "$CONTAINER_IMAGE")
  fi
  if [[ -n "$PRIMARY_ENV" ]]; then
    cmd+=(--offloader-primary-comp-env "$PRIMARY_ENV")
  fi
  if [[ -n "$SECONDARY_ENV" ]]; then
    cmd+=(--offloader-secondary-comp-env "$SECONDARY_ENV")
  fi
  if [[ -n "$PERSISTENT_VOLUMES" ]]; then
    cmd+=(--offloader-persistent-volumes "$PERSISTENT_VOLUMES")
  fi
  if [[ -n "$NAMESPACE" ]]; then
    cmd+=(--offloader-namespace "$NAMESPACE")
  fi
  if [[ -n "$WORKDIR" ]]; then
    cmd+=(--offloader-shared-workdir "$WORKDIR")
  fi
  if [[ ${#SHARED_FS_USAGE[@]} -gt 0 ]]; then
    cmd+=(--shared-fs-usage "${SHARED_FS_USAGE[@]}")
  fi
  if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    cmd+=("${EXTRA_ARGS[@]}")
  fi

  echo ""
  echo "=== Run ${i}/${RUNS} ==="
  echo "Workflow: ${WORKFLOW_NAME}"
  echo "Workdir : ${WORKDIR}"

  local status=0
  set +e
  (
    cd "$WORKDIR"
    export XDG_CACHE_HOME="$PWD/.snakemake/xdg-cache"
    mkdir -p "$XDG_CACHE_HOME"
    "${cmd[@]}" > snakemake.log 2>&1
  )
  status=$?
  set -e

  local finished_at
  finished_at="$(date --iso-8601=seconds)"

  archive_run "$i" "$status" "$started_at" "$finished_at"
  cleanup_runtime_dirs

  if [[ "$status" -ne 0 ]]; then
    echo "Run ${i} failed with exit code ${status}."
    if [[ "$CONTINUE_ON_ERROR" -ne 1 ]]; then
      echo "Stopping batch because --continue-on-error is not set."
      return "$status"
    fi
  fi

  return 0
}

echo "Starting batch: workflow=${WORKFLOW_NAME}, runs=${RUNS}, jobs=${JOBS}, max_threads=${MAX_THREADS}"
echo "Archive base: ${ARCHIVE_BASE}"

for ((i=1; i<=RUNS; i++)); do
  run_one "$i"
done

echo "Batch finished."
