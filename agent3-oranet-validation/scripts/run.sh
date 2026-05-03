#!/usr/bin/env bash
# Agent 3 runner — sets up venv, installs deps, runs validation.
#
# Usage:
#   ./scripts/run.sh [path/to/agent3_input.json]
#
# Env vars:
#   AGENT3_OUTPUT       (default: agent4_input.json)
#   AGENT3_REPORT       (default: agent3_report.json)
#   ALLOW_WARNINGS=1    (warnings exit 0)
#   STRICT_PASS=1       (any non-PASS exits 2)
set -euo pipefail
IFS=$'\n\t'

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
INPUT="${1:-$ROOT/agent3_input.json}"
OUT="${AGENT3_OUTPUT:-$ROOT/agent4_input.json}"
REPORT="${AGENT3_REPORT:-$ROOT/agent3_report.json}"
VENV="$ROOT/.venv"

log() { printf '[agent3] %s\n' "$*"; }
fail(){ printf '[agent3] FATAL: %s\n' "$*" >&2; exit 3; }

[[ -f "$INPUT" ]] || fail "input not found: $INPUT (run Agent 2 first)"

command -v python3 >/dev/null || fail "python3 not in PATH"

if [[ ! -d "$VENV" ]]; then
  log "creating venv: $VENV"
  python3 -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$ROOT/requirements.txt"

ARGS=(--input "$INPUT" --output "$OUT" --report "$REPORT")
[[ "${ALLOW_WARNINGS:-0}" == "1" ]] && ARGS+=(--allow-warnings)
[[ "${STRICT_PASS:-0}" == "1" ]] && ARGS+=(--strict-pass)
[[ "${VERBOSE:-0}" == "1" ]] && ARGS+=(-v)

set +e
python3 "$ROOT/agent3.py" "${ARGS[@]}"
RC=$?
set -e

case "$RC" in
  0) log "ready — Agent 4 may proceed. handoff: $OUT" ;;
  1) log "ready with warnings — review $REPORT before continuing" ;;
  2) log "BLOCKED — failures detected. Agent 4 must NOT run. See $REPORT" ;;
  *) log "agent3 errored (rc=$RC). See logs above." ;;
esac
exit "$RC"
