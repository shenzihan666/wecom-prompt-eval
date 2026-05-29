#!/usr/bin/env bash
# Enumerate ALL prompt-upgrade points from Grafana logs.
#
# The system's only invariant is: every AI upgrade ends in a prompt change.
# Those are logged on the management server as "Prompt version created"
# (also "Prompt updated" / "Prompt deleted"). This script lists every such
# event and clusters bursts (within GAP seconds) into discrete "upgrade moments".
#
# Usage: find_upgrades.sh [lookback_days=45] [gap_seconds=900]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$DIR/lib.sh"; load_grafana

LOOKBACK_DAYS="${1:-45}"
GAP="${2:-900}"
NOW=$(date +%s); START=$((NOW - LOOKBACK_DAYS*86400))
Q='{job="app_metrics"} |~ `(?i)prompt version created|prompt updated|prompt deleted`'

echo "Source: Grafana Loki (job=app_metrics) | lookback=${LOOKBACK_DAYS}d | cluster gap=${GAP}s" >&2
echo >&2

# Loki caps a single query at 30d, so walk the window in <=28d chunks.
CHUNK=$((28*86400))
# fields: epoch \t local_str(UTC+8) \t device \t event \t prompt_id
parse='.data.result[] | (.stream.device // "-") as $d | .values[] |
      (.[1]|fromjson? // {}) as $j |
      ((.[0]|tonumber)/1e9 | floor) as $e |
      [ $e, (($e+28800)|todate|sub("T";" ")|sub("Z";"")), $d, ($j.event // "?"), ($j.prompt_id // "-") ] | @tsv'
events="$(
  cs=$START
  while [ "$cs" -lt "$NOW" ]; do
    ce=$((cs + CHUNK)); [ "$ce" -gt "$NOW" ] && ce=$NOW
    loki_range "$Q" "$cs" "$ce" 5000 backward | jq -r "$parse" 2>/dev/null || true
    cs=$ce
  done | sort -u)"   # full-line dedup (epoch is fixed-width, so lexical == chronological)

if [ -z "$events" ]; then echo "No prompt-upgrade events found in window." ; exit 0; fi

# Single source of truth for names = lib.sh server_name(). Build a "dev=name;..."
# map for the devices we actually saw, so awk can render friendly server names.
devs="$(echo "$events" | cut -f3 | sort -u)"
namemap=""; legend=""
for d in $devs; do
  n="$(server_name "$d")"
  namemap="${namemap}${d}=${n};"
  legend="${legend}  ${n} = ${d}\n"
done

echo "================= SERVERS IN WINDOW ================="
echo "(all are production servers; each has its own prompt DB — prompt_id sets do not overlap across servers)"
printf "%b" "$legend"
echo
echo "================= UPGRADE MOMENTS (per server) ================="
printf "%-12s | %-25s | %-8s | %-8s\n" "server" "time (UTC+8)" "events" "prompts"
printf -- "-------------|---------------------------|----------|---------\n"
# Cluster PER DEVICE: sort by device, then epoch; reset on device change or time gap.
echo "$events" | sort -t$'\t' -k3,3 -k1,1n | awk -F'\t' -v gap="$GAP" -v map="$namemap" '
  BEGIN{ n=split(map,a,";"); for(i=1;i<=n;i++){ if(a[i]!=""){ split(a[i],kv,"="); nm[kv[1]]=kv[2] } } }
  {
    ts=$1; tstr=$2; dev=$3; pid=$5;
    if (dev!=cdev || ts-last>gap) {
      if (cnt>0) flush();
      cdev=dev; cstartstr=tstr; cnt=0; delete seenp; np=0;
    }
    cnt++; last=ts; cendstr=tstr;
    if (!(pid in seenp)){ seenp[pid]=1; np++ }
  }
  END{ if(cnt>0) flush() }
  function flush(   sv){
    sv=(cdev in nm)?nm[cdev]:cdev;
    tlabel=(cstartstr==cendstr)?cstartstr:(cstartstr " .. " substr(cendstr,12));
    printf "%-12s | %-25s | %-8s | %-8s\n", sv, tlabel, cnt, np;
  }'

echo
echo "Next: pick a server + one upgrade moment above, then run fetch_turns.py /"
echo "score_compare.py for the before/after windows around that timestamp."
echo
echo "================= RAW EVENTS (time | server | event | prompt_id) ================="
echo "$events" | awk -F'\t' -v map="$namemap" '
  BEGIN{ n=split(map,a,";"); for(i=1;i<=n;i++){ if(a[i]!=""){ split(a[i],kv,"="); nm[kv[1]]=kv[2] } } }
  { sv=($3 in nm)?nm[$3]:$3; printf "%s | %-10s | %-22s | %s\n", $2, sv, $4, $5 }'
