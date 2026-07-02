#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

mkdir -p workflow-runs/ftp-root .sou
echo "ftp ok" > workflow-runs/ftp-root/healthcheck.txt

export SNAKEMAKE_STORAGE_FTP_USERNAME="${SNAKEMAKE_STORAGE_FTP_USERNAME:-snakemake}"
export SNAKEMAKE_STORAGE_FTP_PASSWORD="${SNAKEMAKE_STORAGE_FTP_PASSWORD:-snakemake}"
if [[ -z "${LOCAL_FTP_NAT_ADDRESS:-}" ]]; then
  LOCAL_FTP_NAT_ADDRESS="$(ifconfig | awk '/^en0:/{found=1} found && /inet / && $2 != "127.0.0.1" {print $2; exit}')"
fi
export LOCAL_FTP_NAT_ADDRESS="${LOCAL_FTP_NAT_ADDRESS:-192.168.65.254}"

if docker ps --format '{{.Names}}' | grep -qx snakemake-offloader-ftp; then
  echo "Local FTP Docker container already running"
  exit 0
fi

docker rm -f snakemake-offloader-ftp >/dev/null 2>&1 || true

docker run -d \
  --name snakemake-offloader-ftp \
  -p 2121:2121 \
  -p 30000-30009:30000-30009 \
  -v "$PWD/workflow-runs/ftp-root:/ftp" \
  python:3.11-alpine \
  sh -c "pip install --no-cache-dir pyftpdlib && python -m pyftpdlib -i 0.0.0.0 -p 2121 -u '$SNAKEMAKE_STORAGE_FTP_USERNAME' -P '$SNAKEMAKE_STORAGE_FTP_PASSWORD' -d /ftp -w -n '$LOCAL_FTP_NAT_ADDRESS' -r 30000-30009" \
  > .sou/local-ftp.container

echo "Local FTP Docker container started: $(cat .sou/local-ftp.container)"
echo "Shared FTP URL for Snakemake and pods: ftp://${LOCAL_FTP_NAT_ADDRESS}:2121/"
echo "Loopback FTP URL for macOS-only checks: ftp://127.0.0.1:2121/"
echo "Username: ${SNAKEMAKE_STORAGE_FTP_USERNAME}"
echo "Password: ${SNAKEMAKE_STORAGE_FTP_PASSWORD}"
