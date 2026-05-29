# wecom-prompt-eval

A [Cursor Agent Skill](https://docs.cursor.com/agent/skills) that evaluates whether the
wecom-chatbot AI got better or worse across a prompt upgrade.

It finds every `Prompt version created` event from Grafana Loki, fetches verbatim
customer→AI reply turns before and after a chosen upgrade, and produces a structured
quality report (objective metrics + rubric scoring).

## Prerequisites

This skill bundles its own scripts but **borrows two external dependencies**:

1. **[grafana-dash-builder](https://github.com/shenzihan666/grafana-dash-builder)** — Grafana
   credentials (`GRAFANA_URL` + `GRAFANA_TOKEN`) live in that skill's `.env`; this skill
   connects to Loki through them.
2. **Python with `pandas`** (optional) — only needed for the LangSmith fallback on
   pre-mirror windows. The primary Grafana path uses stdlib only.

## Install

```bash
git clone https://github.com/shenzihan666/wecom-prompt-eval.git \
  ~/.cursor/skills/wecom-prompt-eval
```

Also install `grafana-dash-builder` and configure its `.env` before first use.

## First run

```bash
bash ~/.cursor/skills/wecom-prompt-eval/scripts/preflight.sh
```

Preflight verifies grafana-dash-builder, Grafana connectivity, `jq`/`curl`, and Python,
then prints the `PYEVAL` / `SK` env vars to export.

## What's inside

```
.
├── SKILL.md          # agent instructions
├── reference.md      # data model, pitfalls, env overrides
└── scripts/
    ├── preflight.sh      # dependency gate (run first)
    ├── find_upgrades.sh  # enumerate prompt-upgrade moments from Loki
    ├── fetch_turns.py    # fetch verbatim Q→A turns (Grafana / LangSmith)
    ├── score_compare.py  # objective metrics + report scaffold
    └── lib.sh            # shared Loki helper
```

## Workflow

1. `preflight.sh` — verify deps
2. `find_upgrades.sh` — list upgrade points per server
3. User picks server + before/after windows
4. `fetch_turns.py` — pull verbatim turns into JSONL
5. `score_compare.py` — auto metrics + report scaffold; agent fills rubric

## License

MIT
