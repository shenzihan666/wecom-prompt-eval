#!/usr/bin/env bash
# Shared helpers for the wecom-prompt-eval skill.
# Reuses the Grafana credentials installed by the grafana-dash-builder skill.
set -euo pipefail

# Where the Grafana service-account creds live (GRAFANA_URL + GRAFANA_TOKEN).
# Reused from the grafana-dash-builder skill; honor $GRAFANA_ENV, else take the
# first .env that exists across the .cursor / .claude skill locations.
if [ -z "${GRAFANA_ENV:-}" ]; then
  for _cand in \
    "$HOME/.cursor/skills/grafana-dash-builder/.env" \
    "$HOME/.claude/skills/grafana-dash-builder/.env"; do
    [ -f "$_cand" ] && { GRAFANA_ENV="$_cand"; break; }
  done
  GRAFANA_ENV="${GRAFANA_ENV:-$HOME/.cursor/skills/grafana-dash-builder/.env}"
fi
LOKI_UID="${LOKI_UID:-loki}"

# Device (ECS hostname, the `device` log label) -> friendly server name.
# These are the names from the ops IP/name table. The device<->IP binding was
# verified from each server's own self-served Host header on `event=Request
# started` (a server's incoming requests carry its own public IP in the URL):
#   iZbp1ew197l6kdw5vqrjd9Z = 118.31.238.44  -> weilike   (~34.7k chats/3d, the high-volume production AI)
#   iZbp1avreyhc6vvw7e4qvgZ = 8.136.11.129   -> Brain     (~358 chats/3d, where most prompt edits land)
#   iZ7xvj40167xefk3gsnjodZ = 8.148.241.169  -> sdj        (~36 chats/3d, the Grafana host)
# NOTE: all three are production servers (there is no separate "role"). The
# table name "Brain" is just a label and is NOT the highest-volume AI — pick the
# evaluation target by upgrade timeline + volume, not by the literal name.
server_name() {
  case "$1" in
    iZbp1ew197l6kdw5vqrjd9Z) echo "weilike";;
    iZbp1avreyhc6vvw7e4qvgZ) echo "Brain";;
    iZ7xvj40167xefk3gsnjodZ) echo "sdj";;
    *) echo "$1";;
  esac
}

load_grafana() {
  [ -f "$GRAFANA_ENV" ] || { echo "ERROR: Grafana .env not found at $GRAFANA_ENV (set GRAFANA_ENV)" >&2; exit 3; }
  set -a; # shellcheck disable=SC1090
  source "$GRAFANA_ENV"; set +a
  : "${GRAFANA_URL:?GRAFANA_URL missing in $GRAFANA_ENV}"
  : "${GRAFANA_TOKEN:?GRAFANA_TOKEN missing in $GRAFANA_ENV}"
  GRAFANA_URL="${GRAFANA_URL%/}"
  export GRAFANA_URL GRAFANA_TOKEN
}

# loki_range <query> <start_epoch_s> <end_epoch_s> [limit] [direction]
loki_range() {
  local q="$1" s="$2" e="$3" limit="${4:-1000}" dir="${5:-backward}"
  curl -sS -G "$GRAFANA_URL/api/datasources/proxy/uid/$LOKI_UID/loki/api/v1/query_range" \
    --data-urlencode "query=$q" \
    --data-urlencode "start=${s}000000000" --data-urlencode "end=${e}000000000" \
    --data-urlencode "limit=$limit" --data-urlencode "direction=$dir" \
    -H "Authorization: Bearer $GRAFANA_TOKEN"
}
