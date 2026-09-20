#!/usr/bin/env bash
# gates.sh — every local check that must pass before a change is merged.
#
# There is no CI, so these ARE the checks. Run from the repo root.
#   ./scripts/gates.sh              ruff + pytest + web typecheck + web build
#   ./scripts/gates.sh --quick      ruff only (fast inner loop)
# Exits 0 only if every gate it ran is green.
#
# The web gates need `npm ci` to have been run in web/ at least once.
# Passing these does not deploy anything.

set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)" || exit 1

# A Windows venv puts the interpreter under Scripts/; POSIX puts it under bin/.
# Try both, then fall back to whatever python3 is on PATH.
PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY=".venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
LOGDIR="${TMPDIR:-/tmp}/aa-gates"
mkdir -p "$LOGDIR"

QUICK=0
for a in "$@"; do
  case "$a" in
    --quick)  QUICK=1 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

declare -a NAMES STATUS DETAIL
RC=0

run_gate () {                       # run_gate <name> <logfile> <command...>
  local name="$1" log="$2"; shift 2
  printf '  running %-14s … ' "$name"
  if "$@" > "$log" 2>&1; then
    echo "ok"
    NAMES+=("$name"); STATUS+=("GREEN"); DETAIL+=("$(basename "$log")")
  else
    echo "FAILED"
    NAMES+=("$name"); STATUS+=("RED"); DETAIL+=("$log")
    RC=1
  fi
}

echo "── gates ──────────────────────────────────────────────"
run_gate "ruff"    "$LOGDIR/ruff.log"  "$PY" -m ruff check .

if [ "$QUICK" -eq 0 ]; then
  PYTHONPATH="$(pwd)" run_gate "pytest" "$LOGDIR/pytest.log" "$PY" -m pytest -q
  run_gate "web-typecheck" "$LOGDIR/tsc.log" npm --prefix web run typecheck
  run_gate "web-build" "$LOGDIR/web.log" npm --prefix web run build
fi

echo
echo "GATES SUMMARY"
printf '%-14s %-6s %s\n' "GATE" "STATE" "LOG"
for i in "${!NAMES[@]}"; do
  printf '%-14s %-6s %s\n' "${NAMES[$i]}" "${STATUS[$i]}" "${DETAIL[$i]}"
done
echo

if [ "$RC" -ne 0 ]; then
  echo "RED — do not merge. Open the log above for the first failure."
  echo "If a failure predates your change, say so in the pull request."
else
  echo "All gates green."
fi
exit "$RC"
