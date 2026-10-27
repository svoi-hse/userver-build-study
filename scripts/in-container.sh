#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$HOME"
trap 'chown -R "${HOST_UID:-0}:${HOST_GID:-0}" /results' EXIT
export DEBIAN_FRONTEND=noninteractive
if ! command -v clang++-18 >/dev/null; then
    apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 update > /results/compiler-install.log 2>&1
    apt-get install -y --no-install-recommends \
        'clang-18=1:18.1.8~++20240731024944+3b5b5c1ec4a3-1~exp1~20240731145000.144' >> /results/compiler-install.log 2>&1
fi
dpkg-query -W > /results/packages.txt
STUDY=$(mktemp -d /tmp/userver-build-study.XXXXXX)
export STUDY

python3 - <<'PY'
import hashlib, json, pathlib
root = pathlib.Path('/work')
expected = json.loads((root / 'versions.json').read_text())['clang_build_analyzer_archive_sha256']
actual = hashlib.sha256((root / 'vendor/ClangBuildAnalyzer.tar.gz').read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit('The ClangBuildAnalyzer archive does not match versions.json')
PY

mkdir -p "$STUDY/analyzer-src"
tar -xzf /work/vendor/ClangBuildAnalyzer.tar.gz -C "$STUDY/analyzer-src" --strip-components=1
cmake -S "$STUDY/analyzer-src" -B "$STUDY/analyzer-build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.10 \
    -DCMAKE_CXX_COMPILER=clang++-18 -DCMAKE_C_COMPILER=clang-18 > /results/analyzer-configure.log 2>&1
cmake --build "$STUDY/analyzer-build" -j 4 > /results/analyzer-build.log 2>&1
export CBA="$STUDY/analyzer-build/ClangBuildAnalyzer"
python3 /work/scripts/compression_probe.py
python3 /work/scripts/experiment.py
