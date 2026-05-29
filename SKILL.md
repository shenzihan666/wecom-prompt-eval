---
name: wecom-prompt-eval
description: >-
  Evaluate whether the wecom-chatbot AI got better or worse across a prompt
  upgrade. Finds every prompt-upgrade point from Grafana logs, then compares
  verbatim AI replies before vs after a chosen upgrade (Grafana
  observability.jsonl as primary source, LangSmith as fallback for older data),
  producing an LLM-scored quality report. Use when asked to assess an AI/prompt
  upgrade, compare AI 升级前后 reply quality, find prompt 升级时间点, or判断升级变好还是变坏
  for the welike-platform wecom-chatbot "AI 大脑".
disable-model-invocation: true
---

# wecom-prompt-eval

Analysis-only workflow (no Grafana dashboards). It answers: **did the AI brain's
replies get better or worse across a prompt upgrade?**

System invariant: every AI upgrade — whatever the mechanism — lands as a **prompt
change**, logged as `Prompt version created`. So upgrades are found from logs,
then reply quality is compared around the chosen point.

## Setup

This skill **bundles its own scripts** (`scripts/find_upgrades.sh`,
`fetch_turns.py`, `score_compare.py`, `lib.sh`) — nothing else to copy. It only
**borrows two external things**, which are deliberately *not* vendored in:

1. **Grafana credentials** — reused from the `grafana-dash-builder` skill's `.env`
   (`GRAFANA_URL` + `GRAFANA_TOKEN`) and its `gf.sh`. That skill must be installed
   and configured; this is how we connect to Grafana. Override the `.env` path via
   `GRAFANA_ENV`.
2. **A Python with `pandas`** — only the **LangSmith fallback** needs it. The
   primary Grafana path is pure stdlib. Default interpreter is the langsmith venv;
   override via `PYEVAL`.

**Always run the preflight first** — it verifies both of the above (plus
`jq`/`curl`) and prints the exact `PYEVAL`/`SK` to export:

```bash
bash ~/.cursor/skills/wecom-prompt-eval/scripts/preflight.sh
# then, as the preflight suggests:
export PYEVAL=...        # python it resolved (langsmith venv, or python3)
export SK=~/.cursor/skills/wecom-prompt-eval/scripts
```

If the preflight reports a hard FAIL (no `grafana-dash-builder`, missing/empty
`GRAFANA_URL`/`GRAFANA_TOKEN`, can't reach Grafana, or no `jq`/`curl`/`python3`),
**stop and fix it before continuing** — the rest of the workflow can't run.

- Servers (all production; `device` log label = ECS hostname → friendly name in
  the ops table). Verified from each server's self-served Host header + chat volume:
  - `iZbp1ew197l6kdw5vqrjd9Z` = **weilike** (118.31.238.44) — ~34.7k chats/3d, the high-volume production AI.
  - `iZbp1avreyhc6vvw7e4qvgZ` = **Brain** (8.136.11.129) — ~358 chats/3d, where most prompt edits land.
  - `iZ7xvj40167xefk3gsnjodZ` = **sdj** (8.148.241.169) — ~36 chats/3d, the Grafana host.
  - ⚠️ The table name "Brain" is just a label — it is **not** the highest-volume AI
    (that's "weilike"). Don't pick the eval target by name; pick by upgrade timeline
    + volume. The mapping lives in `scripts/lib.sh` (`server_name`).
  - Loki datasource uid = `loki`; observability mirror stream = `service="wecom-chatbot"`.

## Workflow

```
- [ ] Step 0: Preflight — verify external deps (grafana-dash-builder + .env, python, jq/curl)
- [ ] Step 1: Enumerate ALL upgrade points
- [ ] Step 2: Ask the user which upgrade / which two windows to compare
- [ ] Step 3: Fetch verbatim turns for the BEFORE and AFTER windows
- [ ] Step 4: Score & compare; write the report
- [ ] Step 5: Interpret with caveats
```

### Step 0 — Preflight (required)

```bash
bash $SK/preflight.sh      # or the absolute path before $SK is exported
```

Checks (and does NOT vendor) the external prerequisites:
- the `grafana-dash-builder` skill is installed and its `.env` has a non-empty
  `GRAFANA_URL` + `GRAFANA_TOKEN`, and Grafana is reachable (`gf.sh check`);
- `jq`, `curl`, and a working `python3` are present;
- (soft) `pandas` + the LangSmith parquet, for the fallback only.

Hard FAIL ⇒ fix before proceeding. The script prints the `PYEVAL`/`SK` to export.
Use `PREFLIGHT_SKIP_NET=1` for a config-only (offline) check.

### Step 1 — Enumerate all upgrade points

```bash
bash $SK/find_upgrades.sh 45 900   # lookback_days, cluster_gap_seconds
```

Prints upgrade moments **grouped per server**, showing friendly server names
(weilike / Brain / sdj — see the legend it prints) instead of raw ECS ids. Each
server is independent and has its own prompt DB (`prompt_id` sets don't overlap).
Present this list to the user.

### Step 2 — Let the user choose (do NOT guess)

After listing all upgrade records, **ask the user which server and which upgrade
moment** to evaluate (and the two comparison windows). Do not assume — the
server with the most prompt edits is not necessarily the one with the most
traffic. Help them pick **fair windows**: equal length
and, ideally, the same clock-hours
(traffic is heavily time-of-day dependent — see reference.md). A typical choice:
a stable window just before the upgrade vs an equal window just after the last
`Prompt version created` of that cluster.

### Step 3 — Fetch verbatim turns

```bash
$PYEVAL $SK/fetch_turns.py --start "<BEFORE_START>" --end "<BEFORE_END>" --out /tmp/before.jsonl
$PYEVAL $SK/fetch_turns.py --start "<AFTER_START>"  --end "<AFTER_END>"  --out /tmp/after.jsonl
```

- Times: epoch, `2026-05-29T01:10:00Z` (UTC), bare `2026-05-29T01:10:00` (treated
  as Beijing UTC+8), or `now` / `now-12h`.
- `--source auto` (default): reads Grafana `observability.jsonl` mirror first; if a
  window yields `< --min-grafana` turns (e.g. it predates the mirror, deployed
  ~2026-05-28), it **falls back to LangSmith** automatically.
- `--limit-per-agent N` (default 40) caps samples per agent so scoring stays cheap.
- Each output line: `{ts, trace_id, source, agent, stage, intent, confidence,
  model, user_input, last_user_msg, sys_prompt, reply}`.
  - `last_user_msg` — the **verbatim customer turn** that triggered the reply,
    pulled out of both the old `【最后提问】` format and the new
    `<latest_customer_message>` XML. Always score relevance against this, not the
    raw `user_input` (which is a huge `<task>…<conversation_history>` scaffold).
  - `sys_prompt` — the operator's **custom directives** (`不要发表情`, `TT老师→苏南`,
    `工资→待遇` …) when present; use it to judge instruction-following.

### Step 4 — Score & compare

```bash
$PYEVAL $SK/score_compare.py --before /tmp/before.jsonl --after /tmp/after.jsonl --sample 30
```

Pass run metadata so the report is self-describing and auditable:
`--server`, `--upgrade` (anchor time / prompt_id cluster), `--before-raw` /
`--after-raw` (the pre-sampling turn counts printed by `fetch_turns.py`'s
`[grafana] N turns` line — rerun with `--limit-per-agent 1000000` to get the true
window total). Without these the §0 header is blank.

- No external LLM (no key). It writes **two** files:
  - **`./prompt_eval_report.md`** (current folder) — the **final report**:
    §0 run metadata, §1 **objective metrics** (auto), §1b **matched per-agent**
    objective table, §1c **system_prompt 指令遵循率**（auto-measured compliance
    rate + violation examples）, §2 blank rubric table, §3 **样例分类**（好/坏分开：
    §3.1 严重违规 / §3.2 退化点 / §3.3 改善点，每类按严重度排序）, §4
    auto-generated caveats.
  - **`./eval_pairs.md`** — an **intermediate** file with all numbered `B0..`/`A0..`
    pairs (customer's last message front-loaded, operator directives shown,
    context trimmed) + a **blank per-item score table**. Used only for scoring;
    **do not** copy it into the report. (`--out` / `--pairs` override paths.)

**General / objective metrics §1+§1b+§1c compute automatically (no LLM, comparable
across any upgrade):** sample count, reply-length distribution
(mean/median/**p25/p75**/p90/σ + a **bucketed histogram** that exposes the bimodal
~100字开场白 vs 短回复 split the mean would hide),
**duplicate-reply rate** (template-ism), **emoji+sticker rate**, **concrete
money/percentage rate** (a compliance proxy), agent/stage/model distributions, a
**matched per-agent** table over only the agents present on *both* sides (guards
against Simpson's-paradox from a changed agent mix), and a **§1c system_prompt
指令遵循率** — a measured compliance/violation rate over the regex-checkable
operator directives (不发表情 / 开头不加客套词 / 开头不自我介绍 / auto-extracted
词语替换 X→Y), with verbatim violation examples. These are the dependable
backbone; the rubric is the subjective layer on top.

- **You (the model running this skill) finish the report directly:**
  1. Read `./eval_pairs.md`, fill the **per-item score table** (1–5 on relevance /
     completeness / guidance / tone / compliance / overall), then take per-side
     **and per-matched-agent** averages to fill the §2 tables + write the
     conclusion. Lead with `overall` + the matched-agent deltas, not the raw mean.
  2. In §3, **separate good from bad** and sort each bucket by severity (worst
     first). Fill the three sub-tables the script scaffolds:
     - **§3.1 严重违规** — the most serious problems, listed first and on their
       own: hype/illegal earnings promises, blatant off-topic answers, breaking a
       hard operator directive (emoji/sticker despite `不要发表情`, wrong teacher
       name), spammy repeats, harmful/inappropriate content. Tag which side
       (前/后) + agent + severity (高/中), most severe at the top.
     - **§3.2 退化点** — before↔after pairs that got **worse**, sorted by severity
       descending.
     - **§3.3 改善点** — before↔after pairs that got **better**, sorted by
       significance descending.
     Show only verbatim snippets + a one-line reason; write 无 for an empty
     bucket. Keep §3 short — the report must **not** contain the full Q&A dump.
- `--sample N` (default 30) caps pairs per side so scoring stays focused.

### Be heuristic — extend on your own when an edge case isn't covered

The scripts compute a **general, upgrade-agnostic** baseline. They will **not**
anticipate every quirk of a given upgrade. When you (the running model) notice
something the canned metrics miss, **investigate and report it yourself** instead
of stopping at the script output. Use the raw `/tmp/*.jsonl` (jq/python) to
quantify, and add a row to §1/§1b or a bullet to §4. Principles:

- **Judge against `last_user_msg`, not the scaffold.** 假提问/非客户提问 are now
  **auto-filtered** by `score_compare.py` before scoring: when `last_user_msg` is
  actually the agent/system opening boilerplate (e.g. “感谢您信任并选择 WELIKE…”,
  the AI replying to its own system message), a bare `[图片]`/media placeholder, or
  an add-friend/system notice, the turn is dropped from **all** stats + pairs (§0
  lists the per-reason counts + examples). The filter is **anchored at the start
  and conservative**, so a normal customer turn that merely *mentions* WELIKE is
  kept. Use `--keep-fake` to disable; extend `non_customer_reason` in
  `score_compare.py` if a new boilerplate appears. If you still spot a borderline
  non-question that slipped through, note it isn't cleanly scorable rather than
  penalizing relevance.
- **Check operator-directive compliance when `sys_prompt` is present.** §1c now
  **auto-measures a 遵循率/violation rate** for the regex-checkable directives
  (`不要发表情` → emoji/sticker present; 开头客套词 `好的/了解`; 开头自我介绍 like
  A15's `我是…苏南`; auto-extracted 词语替换 `工资→待遇` / `TT老师→苏南`). **Cite
  §1c's rate**, fold its violation examples into §3.1, and **never** write
  "directives correctly executed" — at most "the regex-checkable subset shows
  X% compliance". Semantic directives the regex can't parse (`更口语`,`别太热情`)
  still need a manual spot-check from `/tmp/*.jsonl`.
- **New agents / stages that exist on only one side** (试镜引导, 引导照片, 话题引导,
  照片评分, 账号技术, `AUDITION_RELATED`, `OFF_TOPIC` …) are **new capability**, not
  a like-for-like quality delta — describe them qualitatively, keep them out of
  matched averages.
- **Watch for confounders before crediting/blaming the prompt:** unequal window
  length, different clock-hours (time-of-day), source mix (grafana vs langsmith),
  model differences, and tiny per-agent n (3/6/8 → anecdote, not a rate). §4
  auto-flags the ones it can detect; add any others you spot.
- **If a metric would mislead, say so and propose a better one.** Reply length is
  weak (shorter ≠ worse); a fixed-template opener inflates length and dup-rate.
  Invent task-appropriate metrics (e.g. "% replies that advance the funnel toward
  留照/试镜", off-topic recovery quality) when they sharpen the verdict.

The bar: a reader should be able to tell **why** you concluded ↑/↓, with the
objective tables + a few verbatim samples backing every claim — and any boundary
case you hit should appear in the report, not be silently dropped.

### Step 5 — Interpret

The finished `./prompt_eval_report.md` should state whether overall ↑/↓ (anchored
on the **matched per-agent** comparison, not the raw cross-agent mean), call out
any dimension or agent that regressed, and back claims with the §1/§1b objective
tables + the **categorized samples** in §3 — 严重违规 first, then 退化点, then
改善点, each sorted worst/most-significant first (not a full dump). §4 caveats are
**auto-generated** from the windows/sources/distributions; read them and **add any
extra confounder or edge case you found** while scoring (see the heuristic block in
Step 4). Especially keep the reminders from reference.md "Pitfalls" — traffic /
time-of-day confounding, source mix, and tiny-n agents. The full pairs + per-item
score table stay in the intermediate `./eval_pairs.md`.

## Scripts

All scripts live under `scripts/` and ship with the skill (self-contained):

- `scripts/preflight.sh` — verify external deps (grafana-dash-builder `.env` + connectivity, jq/curl, python+pandas, parquet) before anything else. `PREFLIGHT_SKIP_NET=1` for offline/config-only.
- `scripts/find_upgrades.sh [lookback_days=45] [gap_s=900]` — enumerate & cluster upgrade points.
- `scripts/fetch_turns.py --start --end [--source auto|grafana|langsmith] [--out]` — verbatim Q→A turns; also derives `last_user_msg` + `sys_prompt`.
- `scripts/score_compare.py --before --after [--out ./report.md] [--pairs ./eval_pairs.md] [--sample N] [--server S] [--upgrade STR] [--before-raw N] [--after-raw N] [--keep-fake]` — auto-filters 假提问/非客户提问 (agent opening boilerplate / media-only / system notices; `--keep-fake` to disable) then writes final report (§0 metadata, §1 objective metrics, §1b matched-agent, §1c system_prompt 指令遵循率 = auto-measured compliance rate + violation examples, §2 rubric scaffold, §3 categorized-samples scaffold = 严重违规/退化点/改善点 sorted by severity, §4 auto-caveats) + intermediate pairs with a per-item score table; the running model fills the rubric & categorized examples (no external LLM).
- `scripts/lib.sh` — shared Loki helper (sourced by find_upgrades.sh).

The objective metrics are a **baseline, not a ceiling** — extend them per the
heuristic block in Step 4 when an upgrade has quirks the scripts don't cover.

## Additional resources

- Data model, field reference, env overrides, and pitfalls: [reference.md](reference.md)
