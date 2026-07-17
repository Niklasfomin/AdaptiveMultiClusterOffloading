#!/usr/bin/env bash
set -euo pipefail

WITH_CONTENTION=false
WITH_FULL_SCORES=false
CONTENTION_RUNTIME=${CONTENTION_RUNTIME:-45}
NET_HOST=${NET_HOST:-}
NET_HOST_MAP=${NET_HOST_MAP:-}
NET_PROBE_DURATION=${NET_PROBE_DURATION:-5}
NET_CONNECT_TIMEOUT=${NET_CONNECT_TIMEOUT:-5}
IPERF3_SERVER_PID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --contention)
      WITH_CONTENTION=true
      shift
      ;;
    --scores-full)
      WITH_FULL_SCORES=true
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: lotaru-g.sh [--contention] [--scores-full]

Options:
  --contention       Run additional contended benchmark and emit contention metrics.
  --scores-full      Also emit memory_score and network_score_mbps (iperf3 probe).

Environment for --scores-full:
  NET_HOST_MAP          comma-separated node=peer-ip mapping supplied by SOU
  NET_PROBE_DURATION    seconds for iperf3 probe (default: 5)
  NET_CONNECT_TIMEOUT   connection timeout seconds (default: 5)
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: $0 [--contention] [--scores-full]" >&2
      exit 1
      ;;
  esac
done

for cmd in sysbench fio python3 awk hostname nproc; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
done

if [[ "$WITH_FULL_SCORES" == true ]]; then
  for cmd in iperf3 timeout; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      echo "Missing required command for --scores-full: $cmd" >&2
      exit 1
    fi
  done
fi

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

  if [[ -n "$IPERF3_SERVER_PID" ]]; then
    kill "$IPERF3_SERVER_PID" >/dev/null 2>&1 || true
    wait "$IPERF3_SERVER_PID" 2>/dev/null || true
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

select_net_host() {
  local entry node peer
  IFS=',' read -ra entries <<< "$NET_HOST_MAP"
  for entry in "${entries[@]}"; do
    node=${entry%%=*}
    peer=${entry#*=}
    if [[ "$node" == "$HOST" ]]; then
      NET_HOST=$peer
      return 0
    fi
  done
  echo "No network peer configured for node ${HOST}" >&2
  return 1
}

start_iperf3_server() {
  if timeout 1s iperf3 -c 127.0.0.1 -t 1 >/dev/null 2>&1; then
    return 0
  fi

  iperf3 -s >/dev/null 2>&1 &
  IPERF3_SERVER_PID=$!
  sleep 1

  if ! kill -0 "$IPERF3_SERVER_PID" >/dev/null 2>&1; then
    echo "Using existing iperf3 server on port 5201" >&2
    IPERF3_SERVER_PID=""
  fi
}

check_net_reachable() {
  if ! timeout "${NET_CONNECT_TIMEOUT}s" iperf3 -c "$NET_HOST" -t 1 >/dev/null 2>&1; then
    echo "Cannot reach iperf3 server at ${NET_HOST}:5201" >&2
    return 1
  fi
}

probe_net_max_mbps() {
  check_net_reachable || return 1

  local probe_timeout=$((NET_PROBE_DURATION + NET_CONNECT_TIMEOUT + 2))
  local probe_output
  probe_output=$(timeout "${probe_timeout}s" iperf3 -c "$NET_HOST" -t "$NET_PROBE_DURATION" -J 2>/dev/null || true)
  if [[ -z "$probe_output" ]]; then
    echo "iperf3 probe failed for host ${NET_HOST}" >&2
    return 1
  fi

  python3 -c 'import json, sys
try:
    data = json.load(sys.stdin)
    end = data.get("end", {})
    bps = (
        end.get("sum_received", {}).get("bits_per_second")
        or end.get("sum_sent", {}).get("bits_per_second")
        or 0
    )
    print(round(max(0, bps / 1_000_000), 3))
except Exception:
    raise SystemExit("Could not parse network score from iperf3 JSON output")' <<< "$probe_output"
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

if [[ "$WITH_FULL_SCORES" == true ]]; then
  select_net_host
  start_iperf3_server
fi

read -r CPU_SCORE RAM_SCORE READ_IOPS WRITE_IOPS IO_SCORE < <(run_benchmark "$FIO_FILE")

RESOURCE_JSON=""
if [[ "$WITH_FULL_SCORES" == true ]]; then
  NETWORK_SCORE_MBPS=$(probe_net_max_mbps)
  RESOURCE_JSON=",
  \"memory_score\": $RAM_SCORE,
  \"network_score_mbps\": $NETWORK_SCORE_MBPS,
  \"network_target_host\": \"$NET_HOST\""
fi

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
  "io_score": $IO_SCORE$RESOURCE_JSON$CONTENTION_JSON,
  "profile_time_s": $((STOP-START))
}
EOF

echo "wrote $OUT/$HOST.rich.json"
echo "lotaru-g.csv is rebuilt by the collector pod"
