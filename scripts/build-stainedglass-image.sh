#!/usr/bin/env bash
# Build the StainedGlass workflow container image.
# Honors TAG and PLATFORM env vars. Falls back to docker buildx if available.
set -euo pipefail

cd "$(dirname "$0")/.."

TAG="${TAG:-snakemake-stainedglass:v9.23.0-linux}"
PLATFORM="${PLATFORM:-linux/amd64}"
DOCKERFILE="${DOCKERFILE:-workflow-runs/stained-glass/Dockerfile.stainedglass}"

echo "Building ${TAG} for ${PLATFORM} from ${DOCKERFILE}"
if docker buildx version >/dev/null 2>&1; then
  docker buildx build \
    --platform "${PLATFORM}" \
    --load \
    -t "${TAG}" \
    -f "${DOCKERFILE}" \
    .
else
  docker build \
    -t "${TAG}" \
    -f "${DOCKERFILE}" \
    .
fi

docker images | grep -F "${TAG}" || true
