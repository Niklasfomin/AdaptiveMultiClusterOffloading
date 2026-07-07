#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"
OUT_DIR="${OUT_DIR:-./lotaru-results}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

kubectl apply -n "$NAMESPACE" -f "$SCRIPT_DIR/lotaru-g-daemonset.yaml"
kubectl apply -n "$NAMESPACE" -f "$SCRIPT_DIR/lotaru-g-results-pod.yaml"

kubectl wait -n "$NAMESPACE" --for=condition=Ready pod/lotaru-benchmark-results --timeout=120s

NODE_COUNT=$(kubectl get nodes --no-headers | wc -l | tr -d ' ')
DEADLINE=$((SECONDS + TIMEOUT_SECONDS))

echo "waiting for $NODE_COUNT benchmark result files..."
while true; do
  RESULT_COUNT=$(kubectl exec -n "$NAMESPACE" lotaru-benchmark-results -- sh -c 'find /results -maxdepth 1 -name "*.rich.json" 2>/dev/null | wc -l | tr -d " "')
  if [[ "$RESULT_COUNT" -ge "$NODE_COUNT" ]]; then
    break
  fi
  if [[ "$SECONDS" -ge "$DEADLINE" ]]; then
    echo "timed out waiting for results: $RESULT_COUNT/$NODE_COUNT files available" >&2
    exit 1
  fi
  sleep 10
done

rm -rf "$OUT_DIR"
kubectl cp -n "$NAMESPACE" lotaru-benchmark-results:/results "$OUT_DIR"

echo "copied results to $OUT_DIR"
echo "delete benchmark resources with:"
echo "kubectl delete -n $NAMESPACE -f $SCRIPT_DIR/lotaru-g-daemonset.yaml"
echo "kubectl delete -n $NAMESPACE -f $SCRIPT_DIR/lotaru-g-results-pod.yaml"
