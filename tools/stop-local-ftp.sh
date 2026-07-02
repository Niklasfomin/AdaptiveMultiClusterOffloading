#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if docker ps -a --format '{{.Names}}' | grep -qx snakemake-offloader-ftp; then
  docker rm -f snakemake-offloader-ftp
  echo "Stopped local FTP Docker container"
else
  echo "No local FTP Docker container found"
fi

rm -f .sou/local-ftp.container .sou/local-ftp.pid
