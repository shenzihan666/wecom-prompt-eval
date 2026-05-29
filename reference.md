# wecom-prompt-eval — reference

## Servers (Loki `device` label → ops name)

All three are **production** servers (no separate "role"). The `device` label is
the ECS hostname; the friendly name comes from the ops IP/name table. The
device↔IP binding was verified from each server's own self-served Host header on
`event="Request started"` (incoming requests carry the server's own public IP in
`url`) and cross-checked against chat volume.

| device (ECS hostname) | name | IP | chats / 3d | note |
|---|---|---|---|---|
| `iZbp1ew197l6kdw5vqrjd9Z` | **weilike** | 118.31.238.44 | ~34,751 | high-volume production AI; app.log `/root/welike-platform/packages/wecom-chatbot/logs/app.log` |
| `iZbp1avreyhc6vvw7e4qvgZ` | **Brain** | 8.136.11.129 | ~358 | where most prompt edits land (heavy prompt-tuning) |
| `iZ7xvj40167xefk3gsnjodZ` | **sdj** | 8.148.241.169 | ~36 | the Grafana host |

⚠️ **Name ≠ volume.** The machine literally named "Brain" is **not** the
highest-volume AI — "weilike" is. Don't anchor by name. The mapping is the single
source of truth in `scripts/lib.sh` (`server_name`); `find_upgrades.sh` renders
these names. To confirm volume empirically: highest count of JSON
`event="Chat completed"` per `device`.

**Servers are independent** — each has its own prompt DB and the `prompt_id`
sets do **not** overlap, so a server's prompt edits do **not** propagate to the
others. Anchor a comparison on the **chosen server's own** `Prompt version
created` events; let the user pick which server + which moment matters (see
SKILL.md Step 2).

## Data sources

### Grafana (primary) — observability.jsonl mirror
Stream `{service="wecom-chatbot"}` (Alloy tails `logs/observability.jsonl`).
`event` label is only `span_start` / `span_end`. A **turn** joins by `trace_id`:

| piece | selector | field |
|---|---|---|
| AI reply (verbatim) | `{service="wecom-chatbot", event="span_end", span_name=~"agent_.*"}` | `outputs.content` (+ `outputs.confidence`, `outputs.model_name`, `outputs.token_count`, `metadata.stage`, `outputs.agent_name`) |
| user message (verbatim) | `{service="wecom-chatbot", event="span_start", span_name="intent_classification"}` | `inputs.user_message` |

Notes:
- `span_end` events carry `outputs` only (`inputs` is null) — the input lives in `span_start`.
- `workflow_node_validate_input` / `workflow_node_classify_intent` span_start also hold `inputs.user_input`, but they dump the **full conversation state** (very large payloads that can truncate Loki responses). Prefer the light `intent_classification` stream. `fetch_turns.py` recursively splits any window whose response is too large.
- The app.log stream (`{job="app_metrics", device=...}`) has summary metrics only: `event="Chat completed"` → `input_length` / `output_length` / `processing_time_ms` / `success`; `event="Processing chat request"` → `user_input` (verbatim) + `request_id`/`session_id`. Useful for cross-period **rates** but not reply text.

### LangSmith (fallback) — older / pre-mirror data
Project `wecom-chatbot` (id `7ca556f2-eb27-4d87-9fe9-dd17c5447609`). Repo:
`~/test_langsmith_mirror_grafana/langsmith-data-analyze/`.
- Cached snapshot: `data/wecom_chatbot_root_runs.parquet` (root runs, verbatim
  `user_input` + `agent_content` + `agent_name` + `agent_confidence` + `predicted_intent`).
- Refresh / extend coverage: `source .env && .venv/bin/python download_all_runs.py --since YYYY-MM-DD`.
- `fetch_turns.py --source auto` uses this automatically when Grafana is thin.

## Upgrade signal
Event JSON `event="Prompt version created"` (also `Prompt updated` / `Prompt deleted`)
in `{job="app_metrics"}`. There are ~8 agent prompts; a "deploy" is a burst of
version-creates within minutes (`find_upgrades.sh` clusters by `gap` seconds).
Loki caps a single query at 30 days, so `find_upgrades.sh` walks in ≤28-day chunks.

## Packaging / external deps
The skill is **self-contained**: `scripts/` ships `preflight.sh`, `find_upgrades.sh`,
`fetch_turns.py`, `score_compare.py`, `lib.sh`. It does **not** vendor the
`grafana-dash-builder` skill (Grafana creds + `gf.sh` are reused from it) nor the
LangSmith venv/parquet. `scripts/preflight.sh` checks those externals are present
& configured before any run — start there (SKILL.md Step 0).

## Env overrides
- `GRAFANA_ENV` — path to the `.env` with `GRAFANA_URL`/`GRAFANA_TOKEN`. Default:
  first existing of `~/.cursor/skills/grafana-dash-builder/.env` then
  `~/.claude/skills/grafana-dash-builder/.env` (the skill is symlinked across both).
- `PYEVAL` — python interpreter for the `.py` scripts (default: the langsmith venv
  if present, else `python3`; `pandas` only needed for the LangSmith fallback).
- `LS_PARQUET` — cached LangSmith parquet path (fallback source); `fetch_turns.py`
  also takes `--ls-parquet`.
- `PREFLIGHT_SKIP_NET=1` — preflight skips the live Grafana round-trip.
- `LOKI_UID` — Loki datasource uid (default `loki`).
- Server name mapping lives in `scripts/lib.sh` (`server_name`); edit there if infra changes.
- `score_compare.py` needs **no** LLM env vars — the model running the skill does the rubric scoring in-context.

## Pitfalls (state these when reporting)
1. **Traffic is heavily time-of-day dependent.** Raw counts/hour mislead if BEFORE
   and AFTER windows cover different clock hours. Use equal-length, same-clock-hour
   windows, and prefer **per-request ratios** for error/quality signals.
2. **The span/observability mirror was deployed ~2026-05-28**, together with the
   recent prompt upgrade. Windows before it have **no Grafana span data** → they
   come from LangSmith. Avoid attributing source differences to the AI.
3. **`confidence` is ~constant 0.9** in this system (≈99.9% of turns) — not a
   quality signal, so it is **not reported** in the quant stats. Rely on the
   rubric + reply content.
4. **`user_input` is a `<task>…` template**; the real customer message is embedded
   inside it. That is the verbatim model input and is fine to score against.
   **But beware 假提问**: sometimes `【最后提问】`/`<latest_customer_message>` is the
   agent's/system's OWN opening boilerplate (e.g. “感谢您信任并选择 WELIKE…”) — the AI
   replying to its own system message — or a bare `[图片]`/system notice, not a
   customer question. `score_compare.py` auto-filters these (`non_customer_reason`,
   anchored & conservative) and lists them in §0; `--keep-fake` disables it.
5. **Reply length alone is weak.** Shorter isn't worse, and the **mean hides
   bimodality** (fixed ~100字开场白 vs very short 试镜/引导照片 replies). Read the
   §1 median/p25/p75 and the **length histogram** (not just the mean), and lead
   with the LLM rubric `overall` + per-dimension deltas, supported by sample pairs.
6. Analysis-only: do **not** create or push Grafana dashboards.
7. **Agent mix changes across upgrades** (new agents like 试镜引导/引导照片/话题引导/
   照片评分/账号技术 appear after the upgrade). A raw cross-agent mean can move purely
   from the mix (Simpson's paradox) — judge升降 on the **matched per-agent** table
   (`score_compare.py` §1b), and treat one-sided agents as new capability, not a
   like-for-like delta. Tiny per-agent n (3/6/8) is anecdote, not a rate.
8. **`sys_prompt` directives are only partially followed.** Operators inject rules
   (`不要发表情`, `TT老师→苏南`, `工资→待遇`, 开头不自我介绍 …). Don't assert they're
   "correctly executed" — `score_compare.py` **§1c now auto-measures a 遵循率/violation
   rate** over the regex-checkable subset: 不发表情 (emoji/sticker present), 开头客套词
   (好的/了解 opener), 开头自我介绍 (e.g. A15's `我是…苏南`), and **auto-extracted 词语替换**
   pairs (`X→Y` / `把X说成Y` / `用Y代替X`), with verbatim violation examples. **Cite that
   rate**, not "正确执行". Semantic directives the regex can't parse (`更口语`, `别太热情`)
   still need a manual spot-check from `/tmp/*.jsonl`; extend the rule set in
   `score_compare.py` (`NAMED_RULES` / `extract_subst`) when a new checkable rule appears.

## Heuristic stance (general metrics + autonomous edge-case handling)
The scripts compute a **general, upgrade-agnostic** objective baseline (length
distribution = mean/median/p25/p75/p90/σ + a bucketed histogram that surfaces the
bimodal opener/短回复 split, duplicate rate, emoji+sticker rate, money/percentage
rate, agent / stage / model distributions, matched per-agent table). This is deliberately not
exhaustive. When the running model meets a boundary case the canned metrics don't
cover, it should **quantify and report it itself** (jq/python over the raw JSONL),
adding a row/bullet to §1/§1b/§4 — rather than stopping at the script output. See
SKILL.md Step 4 "Be heuristic" for the concrete principles.
