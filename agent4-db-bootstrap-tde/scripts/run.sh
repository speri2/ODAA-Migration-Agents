#!/usr/bin/env bash
# Agent 4 launcher — creates a venv, installs deps, runs agent4.py.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="${AGENT4_VENV:-$ROOT/.venv}"
PY="${AGENT4_PYTHON:-python3}"

INPUT="${AGENT4_INPUT:-$ROOT/agent4_input.json}"
OUTPUT="${AGENT4_OUTPUT:-$ROOT/agent5_input.json}"
CONFIG="${AGENT4_CONFIG:-$ROOT/agent4.config.json}"
LOGFILE="${AGENT4_LOG:-$ROOT/agent4.log}"

if [[ ! -d "$VENV" ]]; then
    echo "[agent4] creating venv at $VENV" >&2
    "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$ROOT/requirements.txt"

echo "[agent4] starting at $(date -u +%FT%TZ)" >&2
echo "[agent4]   input  = $INPUT"  >&2
echo "[agent4]   output = $OUTPUT" >&2
echo "[agent4]   config = $CONFIG" >&2
echo "[agent4]   log    = $LOGFILE" >&2

set +e
python -u "$ROOT/agent4.py" \
    --input  "$INPUT" \
    --output "$OUTPUT" \
    --config "$CONFIG" \
    --log-file "$LOGFILE" \
    "$@"
rc=$?
set -e

echo "[agent4] finished at $(date -u +%FT%TZ) rc=$rc" >&2
exit "$rc"
