#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYO3_PYTHON:-python}"

libdir="$("$PY" "$ROOT/scripts/python_env.py" --libdir 2>/dev/null || true)"

if [[ -n "${libdir}" ]]; then
  export DYLD_LIBRARY_PATH="${libdir}${DYLD_LIBRARY_PATH+:$DYLD_LIBRARY_PATH}"
fi

export CARGO_HOME="${CARGO_HOME:-$ROOT/.cargo}"

exec cargo test "$@"
