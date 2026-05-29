#!/usr/bin/env bash
# Preflight dependency check for the wecom-prompt-eval skill.
#
# Run this FIRST, before find_upgrades.sh / fetch_turns.py / score_compare.py.
# It verifies the EXTERNAL prerequisites that are intentionally NOT bundled into
# this skill (so the skill stays small and the Grafana creds live in one place):
#
#   HARD (skill cannot run without these -> non-zero exit):
#     1. The `grafana-dash-builder` skill is installed, and its .env carries a
#        non-empty GRAFANA_URL + GRAFANA_TOKEN.  We connect to Grafana through it.
#     2. Live Grafana connectivity (delegates to grafana-dash-builder/scripts/gf.sh
#        check). Skip with PREFLIGHT_SKIP_NET=1 if you only want a config check.
#     3. jq + curl   (find_upgrades.sh / lib.sh need them).
#     4. A working python3 (fetch_turns.py / score_compare.py run on it; the
#        primary Grafana path is pure stdlib).
#
#   SOFT (only the LangSmith fallback needs these -> warning, never fatal):
#     5. `pandas` importable in the chosen python.
#     6. The cached LangSmith parquet.
#
# Env overrides (all optional):
#   GRAFANA_ENV  path to the .env with GRAFANA_URL/GRAFANA_TOKEN
#                (default: the grafana-dash-builder skill's .env, auto-located).
#   PYEVAL       python interpreter to use for the .py scripts
#                (default: the langsmith venv if present, else `python3`).
#   LS_PARQUET   path to the cached LangSmith parquet (fallback source).
#   PREFLIGHT_SKIP_NET=1   skip the live Grafana round-trip (config-only check).
set -uo pipefail

# ---------- pretty output ----------
if [ -t 1 ]; then
  G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else
  G=""; R=""; Y=""; B=""; N=""
fi
fail=0; warn=0
ok()   { printf "  ${G}[OK]${N}   %s\n" "$*"; }
bad()  { printf "  ${R}[FAIL]${N} %s\n" "$*"; fail=$((fail+1)); }
note() { printf "  ${Y}[WARN]${N} %s\n" "$*"; warn=$((warn+1)); }
hdr()  { printf "\n${B}%s${N}\n" "$*"; }

echo "${B}wecom-prompt-eval — preflight${N}"
echo "(checks the external deps this skill relies on but does not bundle)"

# ---------- locate the grafana-dash-builder skill / .env ----------
hdr "1. grafana-dash-builder skill + Grafana credentials"

GDB_CANDIDATES=(
  "$HOME/.cursor/skills/grafana-dash-builder"
  "$HOME/.claude/skills/grafana-dash-builder"
)
GDB_DIR=""
for d in "${GDB_CANDIDATES[@]}"; do
  if [ -d "$d" ]; then GDB_DIR="$d"; break; fi
done

if [ -n "${GRAFANA_ENV:-}" ]; then
  ENV_FILE="$GRAFANA_ENV"           # explicit override wins
elif [ -n "$GDB_DIR" ]; then
  ENV_FILE="$GDB_DIR/.env"
else
  ENV_FILE="$HOME/.cursor/skills/grafana-dash-builder/.env"
fi

if [ -n "$GDB_DIR" ]; then
  ok "skill found: $GDB_DIR"
else
  bad "grafana-dash-builder skill NOT found in ~/.cursor/skills or ~/.claude/skills."
  echo "         Install it (this skill reuses its Grafana creds + gf.sh) or set GRAFANA_ENV"
  echo "         to a .env that defines GRAFANA_URL + GRAFANA_TOKEN."
fi

if [ -f "$ENV_FILE" ]; then
  ok ".env present: $ENV_FILE"
  # read the two keys without echoing the token
  gurl="$(grep -E '^[[:space:]]*GRAFANA_URL=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
  gtok="$(grep -E '^[[:space:]]*GRAFANA_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
  if [ -n "$gurl" ]; then ok "GRAFANA_URL set ($gurl)"; else bad "GRAFANA_URL missing/empty in $ENV_FILE"; fi
  if [ -n "$gtok" ]; then ok "GRAFANA_TOKEN set (hidden, ${#gtok} chars)"; else bad "GRAFANA_TOKEN missing/empty in $ENV_FILE"; fi
else
  bad ".env not found at $ENV_FILE"
  echo "         Configure the grafana-dash-builder skill first (it creates this .env),"
  echo "         or set GRAFANA_ENV to an existing one."
fi

# ---------- live connectivity (delegates to gf.sh) ----------
hdr "2. Live Grafana connectivity"
GF_SH=""
[ -n "$GDB_DIR" ] && [ -x "$GDB_DIR/scripts/gf.sh" ] && GF_SH="$GDB_DIR/scripts/gf.sh"
if [ "${PREFLIGHT_SKIP_NET:-0}" = "1" ]; then
  note "skipped (PREFLIGHT_SKIP_NET=1)"
elif [ -z "$GF_SH" ]; then
  note "gf.sh not found under grafana-dash-builder/scripts — cannot verify connectivity here."
elif [ -z "${gtok:-}" ] || [ -z "${gurl:-}" ]; then
  note "skipping connectivity check until GRAFANA_URL/GRAFANA_TOKEN are set."
else
  if out="$(bash "$GF_SH" check 2>&1)"; then
    ok "gf.sh check passed:"
    printf '%s\n' "$out" | sed 's/^/         /'
  else
    bad "gf.sh check failed:"
    printf '%s\n' "$out" | sed 's/^/         /'
  fi
fi

# ---------- system tools ----------
hdr "3. System tools (jq, curl)"
for t in jq curl; do
  if command -v "$t" >/dev/null 2>&1; then ok "$t -> $(command -v "$t")"; else bad "$t not on PATH (needed by find_upgrades.sh / lib.sh)"; fi
done

# ---------- python interpreter ----------
hdr "4. Python interpreter"
DEFAULT_VENV="$HOME/test_langsmith_mirror_grafana/langsmith-data-analyze/.venv/bin/python"
PY=""
if [ -n "${PYEVAL:-}" ]; then
  PY="$PYEVAL"
elif [ -x "$DEFAULT_VENV" ]; then
  PY="$DEFAULT_VENV"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
fi

if [ -z "$PY" ]; then
  bad "no python found (set PYEVAL, install python3, or create the langsmith venv)"
elif ! "$PY" -c 'import sys' >/dev/null 2>&1; then
  bad "python at '$PY' does not run"
else
  ok "python -> $PY ($("$PY" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))' 2>/dev/null))"
  # pandas is SOFT: only the LangSmith fallback needs it; the Grafana path is stdlib.
  if "$PY" -c 'import pandas' >/dev/null 2>&1; then
    ok "pandas importable (LangSmith fallback available)"
  else
    note "pandas NOT importable in this python — Grafana path works, but the LangSmith"
    note "       fallback (older/pre-mirror windows) will be unavailable. Use --source grafana,"
    note "       or point PYEVAL at a python with pandas (e.g. the langsmith venv)."
  fi
fi

# ---------- LangSmith parquet (fallback source) ----------
hdr "5. LangSmith parquet (fallback source — optional)"
PARQUET="${LS_PARQUET:-$HOME/test_langsmith_mirror_grafana/langsmith-data-analyze/data/wecom_chatbot_root_runs.parquet}"
if [ -f "$PARQUET" ]; then
  ok "parquet present: $PARQUET"
else
  note "parquet not found: $PARQUET"
  note "       Only needed for the LangSmith fallback on pre-mirror windows. Pass"
  note "       --source grafana (or --ls-parquet PATH) if you don't have it."
fi

# ---------- summary ----------
hdr "Summary"
if [ "$fail" -gt 0 ]; then
  printf "  ${R}%d hard check(s) failed${N}, %d warning(s). Fix the FAIL items before running the skill.\n" "$fail" "$warn"
  echo
  echo "  Tip: this skill bundles its own scripts (find_upgrades.sh, fetch_turns.py,"
  echo "  score_compare.py, lib.sh). It only borrows Grafana creds from grafana-dash-builder."
  exit 1
elif [ "$warn" -gt 0 ]; then
  printf "  ${Y}All hard checks passed${N} (%d warning(s) — LangSmith fallback may be limited).\n" "$warn"
  echo "  You can run the Grafana-sourced workflow. Suggested env:"
  echo "    export PYEVAL=$PY"
  echo "    export SK=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  exit 0
else
  printf "  ${G}All checks passed.${N} Ready to run.\n"
  echo "  Suggested env:"
  echo "    export PYEVAL=$PY"
  echo "    export SK=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  exit 0
fi
