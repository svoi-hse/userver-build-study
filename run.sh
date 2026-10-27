#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
IMAGE='ghcr.io/userver-framework/ubuntu-22.04-userver@sha256:fca9c8ac7e97b4e2b042d822facbb152aec10a9d4e09c0862e44c50640f3f303'
RUNS=${RUNS:-3}
LABEL=${RUN_LABEL:-$(date -u +%Y%m%dT%H%M%SZ)-$$}

case "$RUNS" in 1|2|3|4|5|6|7|8|9|10) ;; *) echo 'RUNS must be between 1 and 10.' >&2; exit 2 ;; esac
if [[ ! "$LABEL" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo 'RUN_LABEL may only contain letters, numbers, underscores and hyphens.' >&2
    exit 2
fi
command -v docker >/dev/null || { echo 'Docker is required.' >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo 'Start Docker Desktop or the Docker daemon first.' >&2; exit 1; }

mkdir -p "$ROOT/results"
OUT="$ROOT/results/$LABEL"
mkdir "$OUT"
printf '%s\n' "$IMAGE" > "$OUT/image.txt"
uname -srm > "$OUT/host.txt"
docker version --format '{{json .}}' > "$OUT/docker-version.json"
docker info --format '{"architecture":{{json .Architecture}},"cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}}}' > "$OUT/docker-host.json"
docker pull --platform linux/amd64 "$IMAGE"
docker image inspect "$IMAGE" > "$OUT/image-inspect.json"

CONTAINER="userver-study-$LABEL"
cleanup() { docker stop "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup INT TERM
docker run --rm --init --name "$CONTAINER" \
    --platform linux/amd64 \
    --cpus=4 --cpuset-cpus=0-3 --memory=8g --memory-swap=8g \
    --env HOME=/tmp/userver-study-home --env RUNS="$RUNS" \
    --env HOST_UID="$(id -u)" --env HOST_GID="$(id -g)" \
    --env CCACHE_DISABLE=1 --env SCCACHE_DISABLE=1 \
    --mount "type=bind,source=$ROOT,target=/work,readonly" \
    --mount "type=bind,source=$OUT,target=/results" \
    "$IMAGE" bash /work/scripts/in-container.sh 2>&1 | tee "$OUT/console.log"
printf '%s\n' "$LABEL" > "$ROOT/results/LATEST"
printf '\nResults: %s\n' "$OUT"
