#!/usr/bin/env bash
# Agent 5 launcher — venv + agent5.py
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="${AGENT5_VENV:-$ROOT/.venv}"
PY="${AGENT5_PYTHON:-python3}"

INPUT="${AGENT5_INPUT:-$ROOT/agent5_input.json}"
OUTPUT="${AGENT5_OUTPUT:-$ROOT/dataguard_state.json}"
CONFIG="${AGENT5_CONFIG:-$ROOT/agent5.config.json}"
LOGFILE="${AGENT5_LOG:-$ROOT/agent5.log}"

if [[ ! -d "$VENV" ]]; then
    echo "[agent5] creating venv at $VENV" >&2
    "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$ROOT/requirements.txt"

echo "[agent5] starting at $(date -u +%FT%TZ)" >&2
echo "[agent5]   input  = $INPUT"  >&2
echo "[agent5]   output = $OUTPUT" >&2
echo "[agent5]   config = $CONFIG" >&2
echo "[agent5]   log    = $LOGFILE" >&2

set +e
python -u "$ROOT/agent5.py" \
    --input  "$INPUT" \
    --output "$OUTPUT" \
    --config "$CONFIG" \
    --log-file "$LOGFILE" \
    "$@"
rc=$?
set -e

echo "[agent5] finished at $(date -u +%FT%TZ) rc=$rc" >&2
exit "$rc"
