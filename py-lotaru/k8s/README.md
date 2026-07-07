# Lotaru-G Kubernetes Benchmark

Build the image:

```bash
docker build -f Dockerfile.lotaru-g -t lotaru-g-benchmark:latest .
```

Run on every node and fetch results to your laptop:

```bash
bash k8s/run-lotaru-g-benchmark.sh
```

The script applies:

- `lotaru-g-daemonset.yaml` — runs `lotaru-g.sh --contention` once per node, then sleeps.
- `lotaru-g-results-pod.yaml` — stable pod used for a single `kubectl cp`.

Local output:

```text
./lotaru-results/<node>.rich.json
./lotaru-results/lotaru-g.csv
```

Override namespace or output directory:

```bash
NAMESPACE=my-namespace OUT_DIR=./results bash k8s/run-lotaru-g-benchmark.sh
```

Cleanup after copying:

```bash
kubectl delete -f k8s/lotaru-g-daemonset.yaml
kubectl delete -f k8s/lotaru-g-results-pod.yaml
```

## Output location

Cluster output is written to a shared `ReadWriteMany` PVC mounted at `/results`.

Each node writes:

```text
/results/<node>.rich.json
```

All nodes update:

```text
/results/lotaru-g.csv
```

Scratch I/O uses host path `/var/tmp/lotaru-fio` to benchmark node-local storage.
