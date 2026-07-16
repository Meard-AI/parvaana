#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PRODUCT_ROOT="$ROOT"
export PRODUCT_DATA="${PRODUCT_DATA:-$ROOT/data}"
export PRODUCT_REPOS="${PRODUCT_REPOS:-$ROOT/repos}"
export PRODUCT_EXPORT="${PRODUCT_EXPORT:-/home/library/code/gitatlas-export}"
export GITATLAS_BIN="${GITATLAS_BIN:-/home/library/gitatlas/gitatlas}"
export GITATLAS_CWD="${GITATLAS_CWD:-/home/library/gitatlas}"
export GITATLAS_HUGEGRAPH_URL="${GITATLAS_HUGEGRAPH_URL:-http://127.0.0.1:18080/graphs/hugegraph}"
export PRODUCT_PORT="${PRODUCT_PORT:-3847}"
cd "$ROOT/backend"
exec "$ROOT/.venv/bin/uvicorn" app:app --host 0.0.0.0 --port "$PRODUCT_PORT"
