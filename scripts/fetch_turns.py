#!/usr/bin/env python3
"""Fetch verbatim AI turns (user_input -> reply) for a time window.

Primary source : Grafana Loki, the `service="wecom-chatbot"` stream that mirrors
                 logs/observability.jsonl. A "turn" joins, by trace_id:
                   - reply : span_end of agent_*   -> outputs.content (+confidence/model/stage)
                   - input : span_start of workflow_node_validate_input -> inputs.user_input
Fallback       : LangSmith (cached parquet from langsmith-data-analyze) when the
                 window predates the Grafana mirror or returns too few turns.

Output: JSONL, one turn per line:
  {ts, trace_id, source, agent, stage, intent, confidence, model, user_input,
   last_user_msg, sys_prompt, reply}

Usage:
  fetch_turns.py --start 2026-05-29T01:10:00 --end now --out after.jsonl
  fetch_turns.py --start 2026-05-24T00:00:00 --end 2026-05-28T14:00:00 --out before.jsonl --source auto
Times: epoch seconds, ISO with `Z` (UTC), bare ISO (treated as Beijing UTC+8),
       or `now` / `now-<N>[d|h|m]`.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def _default_grafana_env() -> str:
    """Grafana creds borrowed from the grafana-dash-builder skill.

    Honor $GRAFANA_ENV, else take the first .env that exists across the
    .cursor / .claude skill locations (the skill is symlinked between them).
    """
    env = os.environ.get("GRAFANA_ENV")
    if env:
        return os.path.expanduser(env)
    for cand in (
        "~/.cursor/skills/grafana-dash-builder/.env",
        "~/.claude/skills/grafana-dash-builder/.env",
    ):
        p = os.path.expanduser(cand)
        if os.path.isfile(p):
            return p
    return os.path.expanduser("~/.cursor/skills/grafana-dash-builder/.env")


DEFAULT_GRAFANA_ENV = _default_grafana_env()
DEFAULT_LS_PARQUET = os.path.expanduser(
    "~/test_langsmith_mirror_grafana/langsmith-data-analyze/data/wecom_chatbot_root_runs.parquet"
)
LOKI_UID = os.environ.get("LOKI_UID", "loki")
CHUNK_S = 28 * 86400


# --------------------------------------------------------------------------- time
def parse_time(s: str) -> int:
    s = s.strip()
    if s == "now":
        return int(time.time())
    if s.startswith("now-"):
        n = s[4:]
        unit = n[-1]
        mult = {"d": 86400, "h": 3600, "m": 60}.get(unit)
        if mult:
            return int(time.time()) - int(n[:-1]) * mult
    if s.isdigit():
        return int(s)
    iso = s.replace(" ", "T")
    if iso.endswith("Z"):
        dt = datetime.fromisoformat(iso[:-1]).replace(tzinfo=timezone.utc)
    elif "+" in iso[10:] or iso[10:].count("-"):
        dt = datetime.fromisoformat(iso)
    else:
        dt = datetime.fromisoformat(iso).replace(tzinfo=CST)  # bare == Beijing
    return int(dt.timestamp())


def iso_cst(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, CST).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------- input parsing
def derive_msg_fields(ui: str | None) -> tuple[str | None, str | None]:
    """Pull (last_customer_message, custom_system_prompt) out of a raw model input.

    Handles both shapes the system uses across the upgrade:
      * old bracket format  -> `…【最后提问】<msg>`
      * new structured XML  -> `<conversation_history>…</…><latest_customer_message><msg></…>`
        with optional `<system_prompt>…</system_prompt>` (operator-defined directives).
    Without this, the verbatim customer turn often sits past score_compare's char
    cap (huge conversation_history), so the reply can't be judged fairly.
    """
    if not ui:
        return None, None
    s = ui.strip()
    sysp = None
    m = re.search(r"<system_prompt>\s*(.*?)(?:</system_prompt>|$)", s, re.S)
    if m and m.group(1).strip():
        sysp = m.group(1).strip()
    last = None
    m = re.search(r"<latest_customer_message>\s*(.*?)\s*</latest_customer_message>", s, re.S)
    if m and m.group(1).strip():
        last = m.group(1).strip()
    if last is None and "CUSTOMER:" in s:
        cust = re.findall(r"^\s*CUSTOMER:\s*(.+?)\s*$", s, re.M)
        if cust:
            last = cust[-1].strip()
    if last is None:
        m = re.search(r"【最后提问】\s*(.+)", s, re.S)
        if m:
            last = m.group(1).strip()
    if last is None:
        lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
        last = lines[-1] if lines else s
    return last, sysp


# --------------------------------------------------------------------------- grafana
def load_grafana_env(path: str) -> tuple[str, str]:
    url = token = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("GRAFANA_URL="):
                url = line.split("=", 1)[1].strip().rstrip("/")
            elif line.startswith("GRAFANA_TOKEN="):
                token = line.split("=", 1)[1].strip()
    if not url or not token:
        sys.exit(f"GRAFANA_URL / GRAFANA_TOKEN missing in {path}")
    return url, token


def _loki_once(url: str, token: str, expr: str, cs: int, ce: int,
               limit: int, direction: str) -> dict:
    params = urllib.parse.urlencode({
        "query": expr, "start": f"{cs}000000000", "end": f"{ce}000000000",
        "limit": str(limit), "direction": direction,
    })
    api = f"{url}/api/datasources/proxy/uid/{LOKI_UID}/loki/api/v1/query_range?{params}"
    req = urllib.request.Request(api, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        body = r.read()
    return json.loads(body)  # may raise on truncated body -> caller splits


def loki_query_range(url: str, token: str, expr: str, start_s: int, end_s: int,
                     limit: int = 5000, direction: str = "backward") -> list:
    """Flat list of (ts_ns, json_obj). Splits any sub-window that returns a
    too-large/truncated body (recurse down to 15-min granularity)."""
    out: list = []

    def walk(cs: int, ce: int) -> None:
        try:
            data = _loki_once(url, token, expr, cs, ce, limit, direction)
        except Exception as e:  # noqa: BLE001  (truncated JSON, timeout, 5xx, ...)
            if ce - cs > 900:  # split until 15 min
                mid = (cs + ce) // 2
                walk(cs, mid)
                walk(mid, ce)
                return
            print(f"  [loki] giving up on {cs}-{ce}: {e}", file=sys.stderr)
            return
        n = 0
        for stream in data.get("data", {}).get("result", []):
            for ts_ns, line in stream.get("values", []):
                try:
                    out.append((int(ts_ns), json.loads(line)))
                    n += 1
                except Exception:  # noqa: BLE001
                    pass
        # If we hit the row cap, the window likely has more — split for completeness.
        if n >= limit and ce - cs > 900:
            mid = (cs + ce) // 2
            walk(cs, mid)
            walk(mid, ce)

    cs = start_s
    while cs < end_s:
        ce = min(cs + CHUNK_S, end_s)
        walk(cs, ce)
        cs = ce
    return out


def fetch_grafana(url: str, token: str, start_s: int, end_s: int) -> list[dict]:
    replies = loki_query_range(
        url, token,
        '{service="wecom-chatbot", event="span_end", span_name=~"agent_.*"}',
        start_s, end_s,
    )
    # intent_classification span_start carries the verbatim user message in a
    # small payload (user_message), unlike workflow_node_* which dumps full state.
    inputs = loki_query_range(
        url, token,
        '{service="wecom-chatbot", event="span_start", span_name="intent_classification"}',
        start_s, end_s,
    )
    # trace_id -> user message
    in_by_trace: dict[str, str] = {}
    for _ts, j in inputs:
        tid = j.get("trace_id")
        inp = j.get("inputs") or {}
        ui = inp.get("user_message") or inp.get("user_input")
        if tid and ui and tid not in in_by_trace:
            in_by_trace[tid] = ui

    turns: list[dict] = []
    for ts_ns, j in replies:
        o = j.get("outputs") or {}
        meta = j.get("metadata") or {}
        agent = o.get("agent_name") or (j.get("name", "").replace("agent_", ""))
        turns.append({
            "ts": iso_cst(ts_ns / 1e9),
            "trace_id": j.get("trace_id"),
            "source": "grafana",
            "agent": agent,
            "stage": meta.get("stage") or o.get("stage"),
            "intent": None,
            "confidence": o.get("confidence"),
            "model": o.get("model_name"),
            "user_input": in_by_trace.get(j.get("trace_id")),
            "reply": o.get("content"),
        })
    return [t for t in turns if t["reply"]]


# --------------------------------------------------------------------------- langsmith
def fetch_langsmith(start_s: int, end_s: int, parquet: str) -> list[dict]:
    try:
        import pandas as pd
    except ImportError:
        sys.exit("pandas required for LangSmith fallback. Run with the langsmith venv "
                 "python (see SKILL.md), or pass --source grafana.")
    if not os.path.exists(parquet):
        print(f"  [langsmith] parquet not found: {parquet}\n"
              f"  Run download_all_runs.py in langsmith-data-analyze first.", file=sys.stderr)
        return []
    df = pd.read_parquet(parquet)
    df["start_time"] = pd.to_datetime(df["start_time"], utc=True)
    lo = datetime.fromtimestamp(start_s, timezone.utc)
    hi = datetime.fromtimestamp(end_s, timezone.utc)
    m = (df["start_time"] >= lo) & (df["start_time"] < hi) & df["agent_content"].notna()
    sub = df[m]
    turns: list[dict] = []
    for _, r in sub.iterrows():
        turns.append({
            "ts": r["start_time"].tz_convert(CST).strftime("%Y-%m-%d %H:%M:%S"),
            "trace_id": str(r.get("trace_id") or r.get("id")),
            "source": "langsmith",
            "agent": r.get("agent_name"),
            "stage": r.get("conversation_stage"),
            "intent": r.get("predicted_intent"),
            "confidence": (float(r["agent_confidence"]) if r.get("agent_confidence") is not None
                           and str(r.get("agent_confidence")) != "nan" else None),
            "model": None,
            "user_input": r.get("user_input"),
            "reply": r.get("agent_content"),
        })
    return turns


# --------------------------------------------------------------------------- sampling
def sample_per_agent(turns: list[dict], n: int, seed: int = 7) -> list[dict]:
    if n <= 0:
        return turns
    by: dict = defaultdict(list)
    for t in turns:
        by[t["agent"] or "?"].append(t)
    rnd = random.Random(seed)
    out: list[dict] = []
    for _agent, items in by.items():
        rnd.shuffle(items)
        out.extend(items[:n])
    out.sort(key=lambda t: t["ts"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--source", choices=["auto", "grafana", "langsmith"], default="auto")
    ap.add_argument("--limit-per-agent", type=int, default=40)
    ap.add_argument("--min-grafana", type=int, default=20,
                    help="auto: fall back to LangSmith if Grafana yields fewer turns")
    ap.add_argument("--out", default=None)
    ap.add_argument("--grafana-env", default=DEFAULT_GRAFANA_ENV)
    ap.add_argument("--ls-parquet", default=DEFAULT_LS_PARQUET)
    args = ap.parse_args()

    start_s, end_s = parse_time(args.start), parse_time(args.end)
    if end_s <= start_s:
        sys.exit("end must be after start")
    print(f"[window] {iso_cst(start_s)} .. {iso_cst(end_s)} (UTC+8)", file=sys.stderr)

    turns: list[dict] = []
    used = args.source
    if args.source in ("auto", "grafana"):
        url, token = load_grafana_env(args.grafana_env)
        turns = fetch_grafana(url, token, start_s, end_s)
        used = "grafana"
        print(f"[grafana] {len(turns)} turns", file=sys.stderr)
        if args.source == "auto" and len(turns) < args.min_grafana:
            print(f"[auto] grafana < {args.min_grafana}; falling back to LangSmith", file=sys.stderr)
            ls = fetch_langsmith(start_s, end_s, args.ls_parquet)
            print(f"[langsmith] {len(ls)} turns", file=sys.stderr)
            if len(ls) > len(turns):
                turns, used = ls, "langsmith"
    else:
        turns = fetch_langsmith(start_s, end_s, args.ls_parquet)
        used = "langsmith"
        print(f"[langsmith] {len(turns)} turns", file=sys.stderr)

    turns = sample_per_agent(turns, args.limit_per_agent)
    for t in turns:  # surface the verbatim customer turn + operator directives
        t["last_user_msg"], t["sys_prompt"] = derive_msg_fields(t.get("user_input"))
    by_agent: dict = defaultdict(int)
    for t in turns:
        by_agent[t["agent"] or "?"] += 1
    print(f"[result] source={used} sampled={len(turns)} agents={dict(by_agent)}", file=sys.stderr)

    out = sys.stdout if not args.out else open(args.out, "w", encoding="utf-8")
    for t in turns:
        out.write(json.dumps(t, ensure_ascii=False) + "\n")
    if args.out:
        out.close()
        print(f"[written] {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
