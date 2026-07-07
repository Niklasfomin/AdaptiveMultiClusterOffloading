#!/usr/bin/env bash
set -euo pipefail

WITH_CONTENTION=false
CONTENTION_RUNTIME=${CONTENTION_RUNTIME:-45}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --contention)
      WITH_CONTENTION=true
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--contention]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: $0 [--contention]" >&2
      exit 1
      ;;
  esac
done

for cmd in sysbench fio python3 awk flock hostname nproc; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
done

HOST="${NODE_NAME:-$(hostname -s)}"
OUT="${OUT:-.}"
SCRATCH="${SCRATCH:-/tmp/lotaru-fio}"

mkdir -p "$OUT" "$SCRATCH"

START=$(date +%s)
CORES=$(nproc)
MEM_GB=$(awk '/MemTotal/{printf "%.0f",$2/1024/1024}' /proc/meminfo)
FIO_FILE="$SCRATCH/fiofile-$HOST"
CONTENTION_FILE="$SCRATCH/contention-$HOST"
CONTENT_PID_FILE="$SCRATCH/contention-pids-$HOST"

cleanup() {
  if [[ -f "$CONTENT_PID_FILE" ]]; then
    while read -r pid; do
      if [[ -n "$pid" ]]; then
        kill "$pid" >/dev/null 2>&1 || true
      fi
    done < "$CONTENT_PID_FILE"
    rm -f "$CONTENT_PID_FILE"
  fi
  rm -f "$FIO_FILE" "$CONTENTION_FILE"
}
trap cleanup EXIT

json_value() {
  local key=$1
  python3 -c 'import json,sys
key = sys.argv[1]
s = sys.stdin.read()
start = s.find("{")
if start < 0:
    raise SystemExit("fio produced no JSON output")
data = json.loads(s[start:])
print(round(data["jobs"][0][key]["iops"]))' "$key"
}

run_benchmark() {
  local fio_file=$1

  local cpu_score
  cpu_score=$(sysbench cpu --threads=1 --time=10 --cpu-max-prime=20000 run \
    | awk '/events per second/{print $4}')

  local ram_score
  ram_score=$(sysbench memory --threads=1 --memory-block-size=1M --memory-total-size=100G run \
    | awk '/MiB transferred/{gsub(/[()]/,"",$4); print $4}')

  fio --name=prep --filename="$fio_file" --size=1G --rw=write --bs=1M \
    --direct=1 --numjobs=1 --iodepth=32 --end_fsync=1 --output-format=json >/dev/null

  local read_json
  read_json=$(fio --name=read --filename="$fio_file" --rw=read --bs=128k \
    --direct=1 --runtime=10 --time_based --iodepth=32 --output-format=json)

  local write_json
  write_json=$(fio --name=write --filename="$fio_file" --rw=write --bs=128k \
    --direct=1 --runtime=10 --time_based --iodepth=32 --output-format=json)

  local read_iops
  read_iops=$(printf '%s' "$read_json" | json_value read)

  local write_iops
  write_iops=$(printf '%s' "$write_json" | json_value write)

  local io_score
  io_score=$(python3 - <<EOF
read_iops = float("$read_iops")
write_iops = float("$write_iops")
print(round((read_iops + write_iops) / 2.0, 3))
EOF
)

  printf '%s %s %s %s %s\n' "$cpu_score" "$ram_score" "$read_iops" "$write_iops" "$io_score"
}

start_contention() {
  : > "$CONTENT_PID_FILE"

  local load_threads=$((CORES - 1))
  if [[ "$load_threads" -lt 1 ]]; then
    load_threads=1
  fi

  sysbench cpu --threads="$load_threads" --time="$CONTENTION_RUNTIME" --cpu-max-prime=20000 run >/dev/null 2>&1 &
  echo "$!" >> "$CONTENT_PID_FILE"

  sysbench memory --threads=1 --time="$CONTENTION_RUNTIME" --memory-block-size=1M --memory-total-size=1T run >/dev/null 2>&1 &
  echo "$!" >> "$CONTENT_PID_FILE"

  fio --name=contention --filename="$CONTENTION_FILE" --size=1G --rw=randrw --bs=128k \
    --direct=1 --runtime="$CONTENTION_RUNTIME" --time_based --iodepth=32 --output-format=json >/dev/null 2>&1 &
  echo "$!" >> "$CONTENT_PID_FILE"

  sleep 2
}

read -r CPU_SCORE RAM_SCORE READ_IOPS WRITE_IOPS IO_SCORE < <(run_benchmark "$FIO_FILE")

CONTENTION_SCORE=""
CONTENTION_JSON=""
if [[ "$WITH_CONTENTION" == true ]]; then
  start_contention
  read -r CONTENDED_CPU_SCORE CONTENDED_RAM_SCORE CONTENDED_READ_IOPS CONTENDED_WRITE_IOPS CONTENDED_IO_SCORE < <(run_benchmark "$FIO_FILE")

  CONTENTION_METRICS=$(python3 - <<EOF
cpu = float("$CPU_SCORE")
ram = float("$RAM_SCORE")
read = float("$READ_IOPS")
write = float("$WRITE_IOPS")
ccpu = float("$CONTENDED_CPU_SCORE")
cram = float("$CONTENDED_RAM_SCORE")
cread = float("$CONTENDED_READ_IOPS")
cwrite = float("$CONTENDED_WRITE_IOPS")

def ratio(contended, baseline):
    return contended / baseline if baseline else 0.0

cpu_ratio = ratio(ccpu, cpu)
ram_ratio = ratio(cram, ram)
read_ratio = ratio(cread, read)
write_ratio = ratio(cwrite, write)
contention_score = (cpu_ratio + ram_ratio + read_ratio + write_ratio) / 4.0
print(round(cpu_ratio, 4), round(ram_ratio, 4), round(read_ratio, 4), round(write_ratio, 4), round(contention_score, 4))
EOF
)
  read -r CPU_CONTENTION_RATIO RAM_CONTENTION_RATIO READ_CONTENTION_RATIO WRITE_CONTENTION_RATIO CONTENTION_SCORE <<< "$CONTENTION_METRICS"

  CONTENTION_JSON=",
  \"contended_cpu_events_s\": $CONTENDED_CPU_SCORE,
  \"contended_ram_score\": $CONTENDED_RAM_SCORE,
  \"contended_read_iops\": $CONTENDED_READ_IOPS,
  \"contended_write_iops\": $CONTENDED_WRITE_IOPS,
  \"contended_io_score\": $CONTENDED_IO_SCORE,
  \"cpu_contention_ratio\": $CPU_CONTENTION_RATIO,
  \"ram_contention_ratio\": $RAM_CONTENTION_RATIO,
  \"read_contention_ratio\": $READ_CONTENTION_RATIO,
  \"write_contention_ratio\": $WRITE_CONTENTION_RATIO,
  \"contention_score\": $CONTENTION_SCORE"
fi

STOP=$(date +%s)

cat > "$OUT/$HOST.rich.json" <<EOF
{
  "node": "$HOST",
  "cores": $CORES,
  "memory_gb": $MEM_GB,
  "cpu_events_s": $CPU_SCORE,
  "ram_score": $RAM_SCORE,
  "read_iops": $READ_IOPS,
  "write_iops": $WRITE_IOPS,
  "io_score": $IO_SCORE$CONTENTION_JSON,
  "profile_time_s": $((STOP-START))
}
EOF

LOTARU_CSV="$OUT/lotaru-g.csv"
LOCK_FILE="$OUT/.lotaru-g.lock"
TMP_FILE="$OUT/.lotaru-g.$HOST.tmp"

(
  flock -x 200

  if [[ -f "$LOTARU_CSV" ]]; then
    awk -F',' -v host="$HOST" '
      NR == 1 {
        print "node,cpu_score,io_score,contention_score"
        next
      }
      $1 != host {
        if (NF < 4) {
          print $1 "," $2 "," $3 ","
        } else {
          print $1 "," $2 "," $3 "," $4
        }
      }
    ' "$LOTARU_CSV" > "$TMP_FILE"
  else
    echo "node,cpu_score,io_score,contention_score" > "$TMP_FILE"
  fi

  echo "$HOST,$CPU_SCORE,$IO_SCORE,$CONTENTION_SCORE" >> "$TMP_FILE"
  mv "$TMP_FILE" "$LOTARU_CSV"

) 200>"$LOCK_FILE"

echo "wrote $OUT/$HOST.rich.json"
echo "updated $OUT/lotaru-g.csv"
