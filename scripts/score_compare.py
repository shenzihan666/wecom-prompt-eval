#!/usr/bin/env python3
"""Compare AI reply quality BEFORE vs AFTER an upgrade.

Inputs are two JSONL files produced by fetch_turns.py.

This script does NOT call any external LLM. Scoring is done by the model that is
running the skill (the agent reads the emitted sample pairs and fills the rubric).

It writes TWO files:
  * --out   (default ./prompt_eval_report.md) — the FINAL report:
      - run metadata (server / upgrade anchor / window bounds + duration / counts)
      - objective quant stats (length distribution, duplicate/emoji/money rates,
        agent + stage + model distributions)
      - a matched per-agent objective table (only agents present on BOTH sides)
      - a blank rubric table (filled by the running model)
      - a categorized "样例分类" section (§3.1 严重违规 / §3.2 退化点 / §3.3 改善点,
        each sorted by severity, good vs bad kept apart)
      - auto-generated caveats.
      It must NOT contain the full Q&A dump.
  * --pairs (default ./eval_pairs.md, an INTERMEDIATE file) — every numbered
      BEFORE/AFTER pair (customer's last message front-loaded, operator directives
      shown, context trimmed) PLUS a blank per-item score table so the rubric is
      auditable. Used by the model only to score and pick typicals.

Usage:
  score_compare.py --before before.jsonl --after after.jsonl
  score_compare.py --before b.jsonl --after a.jsonl --sample 30 \
      --server Brain --upgrade "2026-05-29 ~00:45 prompt cluster" \
      --before-raw 191 --after-raw 612
"""
from __future__ import annotations

import argparse
import json
import re
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

DIMS = ["relevance", "completeness", "guidance", "tone", "compliance", "overall"]
DIM_CN = {
    "relevance": "相关切题", "completeness": "信息完整", "guidance": "主动引导",
    "tone": "语气亲和", "compliance": "合规稳妥", "overall": "总体",
}

RUBRIC = (
    "直播招聘场景对话质量评审。对每条回复在 5 个维度打分（1=差, 5=好）：\n"
    "- relevance 相关切题：是否回应客户当前问题/情况\n"
    "- completeness 信息完整：是否给出足够、准确的信息\n"
    "- guidance 主动引导：是否引导下一步（留资/加微/邀约等）\n"
    "- tone 语气亲和：是否自然、亲和、不机械\n"
    "- compliance 合规稳妥：不浮夸承诺收益、不违规、不过度承诺\n"
    "再给 overall 总体（1-5）。"
)

TS_FMT = "%Y-%m-%d %H:%M:%S"
EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF\U0001F900-\U0001FAFF\U00002600-\U000026FF"
    "\U00002700-\U000027BF\U0001F000-\U0001F0FF\u2B00-\u2BFF\u2934\u2935"
    "\uFE0F\u2764\u2122\u2139\u2194-\u21AA]"
)
# WeCom bracket stickers (curated, to not match media placeholders like [图片]/[视频])
STICKER_RE = re.compile(
    r"\[(?:捂脸|OK|可爱|强|弱|呲牙|抱拳|裂开|皱眉|偷笑|害羞|微笑|愉快|憨笑|得意|玫瑰|"
    r"爱心|拥抱|赞|耶|奸笑|坏笑|流泪|大哭|笑哭|嘿哈|机智|加油|心|调皮|鼓掌|庆祝|烟花|"
    r"福|发财|嘴唇|亲亲|色|酷|思考|疑问|惊讶|捂嘴|摸头|拍手|握手|击掌|比心|哇|社会社会|"
    r"旺柴|右哼哼|左哼哼|嘘|衰|骷髅|敲打|再见|擦汗|抠鼻|鄙视|委屈|快哭了|阴险|愤怒|难过|"
    r"睡|可怜|示爱|咖啡|月亮|太阳|礼物|啤酒|蛋糕|胜利|抱抱|拳头|OK手势|发呆)\]"
)
# replies that quote a concrete money/percentage figure (a compliance signal)
MONEY_RE = re.compile(r"\d[\d,.]*\s*(?:元|块钱|块|万|%|％)")


def has_expr(s: str) -> bool:
    return bool(EMOJI_RE.search(s) or STICKER_RE.search(s))


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# --------------------------------------------------------------- input extraction
def derive_msg_fields(ui: str | None) -> tuple[str | None, str | None]:
    """(last_customer_message, custom_system_prompt) from old bracket or new XML input.
    Mirrors fetch_turns.derive_msg_fields so old JSONL (without the fields) still works."""
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


def last_msg(t: dict) -> str:
    v = t.get("last_user_msg")
    if v is None:
        v = derive_msg_fields(t.get("user_input"))[0]
    return v or ""


def sys_prompt(t: dict) -> str:
    v = t.get("sys_prompt")
    if v is None:
        v = derive_msg_fields(t.get("user_input"))[1]
    return v or ""


# ---------------------------------------------------- 假提问 / 非客户提问 过滤
# Some turns have a 【最后提问】/<latest_customer_message> that is NOT a customer
# question but the agent's/system's OWN opening boilerplate (e.g. B3/B11
# “感谢您信任并选择 WELIKE…”) — the AI is replying to its own system message. These
# pollute relevance scoring and must be dropped. The match is anchored at the
# START of the message and conservative, so a normal customer turn that merely
# *mentions* WELIKE is NOT filtered.
_AGENT_OPENING_RES = [
    re.compile(r"^感谢您?(的)?信任[，,、\s]*(并|和|与)?\s*选择"),  # 感谢您信任并选择 WELIKE…
    re.compile(r"^感谢(您|你)(的)?信任"),
    re.compile(r"^欢迎(您|你)?(来到|加入|选择)\s*WELIKE", re.I),
]
# message is ONLY a media/system placeholder (no real text to answer)
_MEDIA_ONLY_RE = re.compile(
    r"^(\s*\[(?:图片|视频|动画表情|表情|语音|文件|位置|链接|名片|聊天记录|红包|转账|小程序|引用)\]\s*)+$"
)
# WeCom add-friend / verification system notices (not a question)
_SYS_NOTICE_RES = [
    re.compile(r"通过了(你|您)的?(朋友)?(验证|申请)"),
    re.compile(r"^(你|您|对方|我)已?(成功)?添加"),
    re.compile(r"现在可以开始(聊天|交流)了?"),
    re.compile(r"^以上是(打招呼|招呼)内容"),
    re.compile(r"^我是通过.*(添加|加).*(你|您)"),
]


def non_customer_reason(msg: str) -> str | None:
    """Return a short reason if `msg` is NOT a real customer question (agent/system
    opening boilerplate, a bare media placeholder, or a system notice); else None."""
    s = (msg or "").strip()
    if not s:
        return "空消息"
    if any(rx.search(s) for rx in _AGENT_OPENING_RES):
        return "经纪人/系统开场白(非客户提问)"
    if _MEDIA_ONLY_RE.match(s):
        return "纯媒体/占位符(无文本可答)"
    if any(rx.search(s) for rx in _SYS_NOTICE_RES):
        return "加好友/系统通知(非提问)"
    return None


def partition_scorable(turns: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Split turns into (scorable, filtered) where filtered = 假提问/非客户提问."""
    keep, drop = [], []
    for t in turns:
        reason = non_customer_reason(last_msg(t))
        (drop.append((t, reason)) if reason else keep.append(t))
    return keep, drop


def clean_ctx(ui: str | None) -> str:
    """Strip the fixed <task>/<context>/<business_background> scaffolding so the
    trimmed excerpt shows real conversation, not boilerplate."""
    if not ui:
        return ""
    s = ui
    for tag in ("task", "context", "scenario", "customer_name", "situation",
                "business_background", "system_prompt", "latest_customer_message"):
        s = re.sub(rf"<{tag}>.*?</{tag}>", "", s, flags=re.S)
    s = re.sub(r"<system_prompt>.*$", "", s, flags=re.S)            # truncated, no close
    s = re.sub(r"<latest_customer_message>.*$", "", s, flags=re.S)  # shown separately
    s = re.sub(r"</?conversation_history[^>]*>", "", s)
    s = re.sub(r"<[^>\n]{1,40}>", "", s)                            # stray simple tags
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# ------------------------------------------------------------------------- metrics
def _vals(xs):
    return [x for x in xs if isinstance(x, (int, float))]


def _avg(xs):
    xs = _vals(xs)
    return round(st.mean(xs), 1) if xs else None


def _quantile(sorted_xs: list[int], q: float):
    if not sorted_xs:
        return None
    return sorted_xs[min(len(sorted_xs) - 1, int(round(q * (len(sorted_xs) - 1))))]


def length_stats(turns: list[dict]) -> dict:
    lens = sorted(len(t.get("reply") or "") for t in turns if (t.get("reply") or "").strip())
    if not lens:
        return {"mean": None, "median": None, "p25": None, "p75": None, "p90": None,
                "sd": None, "min": None, "max": None}
    return {
        "mean": round(st.mean(lens), 1), "median": int(st.median(lens)),
        "p25": _quantile(lens, 0.25), "p75": _quantile(lens, 0.75),
        "p90": _quantile(lens, 0.90),
        "sd": round(st.pstdev(lens), 1) if len(lens) > 1 else 0.0,
        "min": lens[0], "max": lens[-1],
    }


# reply-length histogram buckets — reveals the bimodal opener(~100字)/短回复 split
LEN_BUCKETS = [(0, 30), (31, 60), (61, 100), (101, 150), (151, 250), (251, 10 ** 9)]
LEN_BUCKET_LABELS = ["≤30", "31–60", "61–100", "101–150", "151–250", "250+"]


def len_hist(turns: list[dict]) -> tuple[list[int], int]:
    lens = [len(t.get("reply") or "") for t in turns if (t.get("reply") or "").strip()]
    counts = [sum(1 for x in lens if lo <= x <= hi) for lo, hi in LEN_BUCKETS]
    return counts, len(lens)


def dup_stats(turns: list[dict]) -> dict:
    norm = [re.sub(r"\s+", "", (t.get("reply") or "")) for t in turns]
    norm = [x for x in norm if x]
    c = Counter(norm)
    n = len(norm)
    redundant = n - len(c)  # copies beyond the first occurrence of each reply
    top_text, top_n = (c.most_common(1)[0] if c else ("", 0))
    return {"rate": (redundant / n if n else 0.0), "n": n, "unique": len(c),
            "top_n": top_n, "top_text": top_text}


def rate(turns: list[dict], pattern: re.Pattern) -> float:
    rs = [(t.get("reply") or "") for t in turns]
    rs = [r for r in rs if r.strip()]
    if not rs:
        return 0.0
    return sum(1 for r in rs if pattern.search(r)) / len(rs)


def rate_expr(turns: list[dict]) -> float:
    """% of replies containing a Unicode emoji OR a WeCom bracket sticker."""
    rs = [(t.get("reply") or "") for t in turns if (t.get("reply") or "").strip()]
    if not rs:
        return 0.0
    return sum(1 for r in rs if has_expr(r)) / len(rs)


def quant(turns: list[dict]) -> dict:
    return {
        "n": len(turns),
        "len": length_stats(turns),
        "dup": dup_stats(turns),
        "emoji": rate_expr(turns),
        "money": rate(turns, MONEY_RE),
        "by_agent": dict(Counter(t.get("agent") or "?" for t in turns)),
        "by_model": dict(Counter(t.get("model") or "-" for t in turns)),
        "by_stage": dict(Counter(t.get("stage") or "-" for t in turns)),
        "by_source": dict(Counter(t.get("source") for t in turns)),
    }


# -------------------------------------------------------------------------- format
def pct(x) -> str:
    return f"{100 * x:.0f}%" if isinstance(x, (int, float)) else "-"


def arrow(b, a):
    if b is None or a is None:
        return ""
    d = a - b
    s = "↑" if d > 0 else ("↓" if d < 0 else "→")
    return f"{s}{d:+.1f}"


def arrow_pp(b, a):  # percentage-point delta
    if b is None or a is None:
        return ""
    d = (a - b) * 100
    s = "↑" if d > 0 else ("↓" if d < 0 else "→")
    return f"{s}{d:+.0f}pp"


def parse_ts(s: str):
    try:
        return datetime.strptime(s, TS_FMT)
    except Exception:  # noqa: BLE001
        return None


def window(turns: list[dict]) -> dict:
    ts = sorted(t["ts"] for t in turns if t.get("ts"))
    if not ts:
        return {"start": None, "end": None, "hours": None, "hourset": set()}
    a, b = parse_ts(ts[0]), parse_ts(ts[-1])
    hours = round((b - a).total_seconds() / 3600, 1) if a and b else None
    hourset = set()
    if a and b:
        cur = a.replace(minute=0, second=0, microsecond=0)
        while cur <= b:
            hourset.add(cur.hour)
            cur += timedelta(hours=1)
    return {"start": ts[0], "end": ts[-1], "hours": hours, "hourset": hourset}


# ------------------------------------------------------------------------ sampling
def select_pairs(turns: list[dict], n: int) -> list[dict]:
    """Round-robin across agents so the scored sample covers every agent type
    (avoids the 'first-N-by-time' bias that over-weights one agent)."""
    if n <= 0 or len(turns) <= n:
        out = list(turns)
    else:
        by: dict = defaultdict(list)
        for t in turns:
            by[t.get("agent") or "?"].append(t)
        for k in by:
            by[k].sort(key=lambda t: t.get("ts") or "")
        out = []
        while len(out) < n and any(by.values()):
            progressed = False
            for k in sorted(by):
                if by[k]:
                    out.append(by[k].pop(0))
                    progressed = True
                    if len(out) >= n:
                        break
            if not progressed:
                break
    out.sort(key=lambda t: (t.get("agent") or "?", t.get("ts") or ""))
    return out


def pairs_block(label: str, prefix: str, turns: list[dict]) -> list[str]:
    out = [f"### {label}样本（{len(turns)} 条，编号 {prefix}0..）\n"]
    for k, t in enumerate(turns):
        lm = last_msg(t).strip()[:300]
        sp = sys_prompt(t).strip().replace("\n", " ")[:300]
        ctx = clean_ctx(t.get("user_input")).strip()
        ctx = ctx[-450:].strip()  # most-recent slice
        rp = (t.get("reply") or "").strip()[:1000]
        out.append(f"**{prefix}{k}** · agent=`{t.get('agent')}` · stage=`{t.get('stage')}`")
        out.append(f"- 客户最后一句: {lm}")
        if sp:
            out.append(f"- 经纪人自定义指令(system_prompt): {sp}")
        if ctx:
            out.append(f"- 对话上下文(节选): {ctx}")
        out.append(f"- AI 回复: {rp}\n")
    return out


def score_table(before_sel: list[dict], after_sel: list[dict]) -> list[str]:
    out = ["## 逐条评分表（模型填写 1-5；填完即可据此核算 §2 均值，可审计）\n",
           "| 编号 | agent | relevance | completeness | guidance | tone | compliance | overall |",
           "|---|---|---|---|---|---|---|---|"]
    for prefix, sel in (("B", before_sel), ("A", after_sel)):
        for k, t in enumerate(sel):
            out.append(f"| {prefix}{k} | {t.get('agent')} |  |  |  |  |  |  |")
    return out


# ---------------------------------------------------------------- matched agents
def matched_agent_objective(before: list[dict], after: list[dict]) -> list[str]:
    bb: dict = defaultdict(list)
    aa: dict = defaultdict(list)
    for t in before:
        bb[t.get("agent") or "?"].append(t)
    for t in after:
        aa[t.get("agent") or "?"].append(t)
    shared = sorted(set(bb) & set(aa), key=lambda k: -len(aa[k]))
    if not shared:
        return ["_两侧无共同 agent，无法做配对对比。_\n"]
    out = ["| agent | n(前/后) | 均字数(前→后) | 重复率(前→后) | 含表情(前→后) | 含金额(前→后) |",
           "|---|---|---|---|---|---|"]
    for ag in shared:
        b, a = bb[ag], aa[ag]
        lb, la = length_stats(b)["mean"], length_stats(a)["mean"]
        out.append(
            f"| {ag} | {len(b)}/{len(a)} | {lb}→{la} | "
            f"{pct(dup_stats(b)['rate'])}→{pct(dup_stats(a)['rate'])} | "
            f"{pct(rate_expr(b))}→{pct(rate_expr(a))} | "
            f"{pct(rate(b, MONEY_RE))}→{pct(rate(a, MONEY_RE))} |"
        )
    only_b = sorted(set(bb) - set(aa))
    only_a = sorted(set(aa) - set(bb))
    out.append("")
    out.append(f"- 仅升级前出现的 agent: `{only_b or '无'}`")
    out.append(f"- 仅升级后出现的 agent（新增能力）: `{only_a or '无'}`")
    return out


def matched_agent_rubric_scaffold(before: list[dict], after: list[dict]) -> list[str]:
    shared = sorted(set(t.get("agent") or "?" for t in before)
                    & set(t.get("agent") or "?" for t in after))
    out = ["> 仅在两侧都存在的 agent 上做配对，避免 agent 配比变化造成的混杂（Simpson's paradox）。"
           "用上面逐条评分表里对应 agent 的 overall 取均值填入：\n",
           "| agent | 升级前 overall | 升级后 overall | 变化 |",
           "|---|---|---|---|"]
    for ag in shared:
        out.append(f"| {ag} | _填_ | _填_ | _填_ |")
    return out


# ----------------------------------------------------------------------- caveats
def auto_caveats(qb, qa, wb, wa) -> list[str]:
    c = []
    if set(qb["by_source"]) != set(qa["by_source"]):
        c.append(f"- **数据来源不一致**：升级前 {qb['by_source']} vs 升级后 {qa['by_source']}；"
                 "来源差异可能被误读为 AI 差异。")
    else:
        src = "/".join(sorted(qb["by_source"])) or "?"
        c.append(f"- **数据来源**：两侧均来自 `{src}`。")
    if wb["hours"] and wa["hours"]:
        ratio = max(wb["hours"], wa["hours"]) / max(min(wb["hours"], wa["hours"]), 1e-6)
        tag = "**窗口长度不一致**" if ratio >= 1.25 else "窗口长度"
        c.append(f"- {tag}：升级前 {wb['hours']}h（{wb['start']}→{wb['end']}）"
                 f" vs 升级后 {wa['hours']}h（{wa['start']}→{wa['end']}）。"
                 + ("样本数差异主要来自窗口长度，不能当作流量/质量信号。" if ratio >= 1.25 else ""))
    if wb["hourset"] and wa["hourset"]:
        ov = len(wb["hourset"] & wa["hourset"]) / max(len(wb["hourset"] | wa["hourset"]), 1)
        if ov < 0.5:
            c.append(f"- **时段不同（time-of-day 混杂）**：两窗口钟点重叠仅 {pct(ov)}；"
                     "直播招聘流量与提问类型随时段变化大，均值差异可能由时段而非 prompt 造成。")
    if set(qb["by_agent"]) != set(qa["by_agent"]):
        c.append("- **agent 构成不同**：整体均值跨不同 agent 混合，"
                 "请以上面『同 agent 配对』表为准来判断升降。")
    if set(m for m in qb["by_model"] if m != "-") != set(m for m in qa["by_model"] if m != "-"):
        c.append(f"- **模型分布差异**：前 `{qb['by_model']}` / 后 `{qa['by_model']}`，"
                 "可能混入模型差异。")
    c.append("- **§1c 指令遵循率仅覆盖正则可核对的规则**（不发表情 / 开头不加客套词 / 开头不自我介绍 / "
             "词语替换）；语义类指令（如『更口语』『别太热情』）无法正则核对，须模型人工抽查，"
             "**勿据此断言全部指令被正确执行**。")
    c.append("- **confidence 恒为 ~0.9**，非质量信号，已不纳入统计。")
    c.append("- **rubric 为小样本人工整数打分**：差异 <~0.3 多在噪声内，勿过度解读；"
             "以逐条评分表 + 配对表为准。")
    c.append("- 分析-only：不创建/推送 Grafana 仪表盘。")
    return c


# ===================================================== system_prompt 指令遵循率
# Regex-checkable subset of operator directives. A turn is "applicable" to a rule
# only when that rule is detected in its sys_prompt; 遵循率 = 1 - 违例/适用。
# Substitution rules (X→Y / 把X说成Y / 用Y代替X) are auto-extracted so新经纪人
# 自定义的替换词也能覆盖。Rules the regex can't recognize仍需模型人工抽查。
_NEG = r"(?:不[要能准许可使得应该会]?|别|勿|禁止|不准|不可|避免|杜绝|切勿|严禁)"
_FILLERS = ["好的", "好滴", "好嘞", "了解", "收到", "明白", "嗯嗯", "ok", "OK"]


def _reply_head(reply: str, n: int = 14) -> str:
    return re.sub(r"^[\s，,。.！!~、:：;；\-—]+", "", reply or "")[:n]


def _detect_no_emoji(sp: str) -> bool:
    return bool(re.search(_NEG + r".{0,8}(表情|emoji|贴(?:图|纸)|图标|颜文字)", sp, re.I)) \
        or bool(re.search(r"(表情|emoji|贴(?:图|纸)).{0,8}" + _NEG, sp, re.I))


def _viol_no_emoji(reply: str, sp: str) -> bool:
    return has_expr(reply)


def _detect_no_filler(sp: str) -> bool:
    return (("开头" in sp) or ("开口" in sp) or ("每句" in sp) or ("每条" in sp)) \
        and any(f in sp for f in ["好的", "了解", "收到", "明白", "客套"]) \
        and bool(re.search(_NEG, sp))


def _viol_no_filler(reply: str, sp: str) -> bool:
    head = _reply_head(reply)
    return any(head.startswith(f) for f in _FILLERS)


def _detect_no_selfintro(sp: str) -> bool:
    return (("开头" in sp) or ("每句" in sp) or ("每条" in sp) or ("反复" in sp)) \
        and (("介绍自己" in sp) or ("自我介绍" in sp) or ("自报" in sp)
             or bool(re.search(r"说自己是", sp))) \
        and bool(re.search(_NEG, sp))


def _viol_no_selfintro(reply: str, sp: str) -> bool:
    return bool(re.match(r"^[\s，,。.！!~、:：;；\-—]*(我(?:是|叫|就是|这边是|这里是)|这(?:边|里)是|本人是)",
                         reply or ""))


# (rule_name, detect(sp) -> bool, violated(reply, sp) -> bool)
NAMED_RULES = [
    ("不发表情/贴纸", _detect_no_emoji, _viol_no_emoji),
    ("开头不加客套词(好的/了解…)", _detect_no_filler, _viol_no_filler),
    ("开头不自我介绍", _detect_no_selfintro, _viol_no_selfintro),
]

_TOK = r"[\u4e00-\u9fffA-Za-z0-9·]{1,10}"
_TOK_MULTI = r"[\u4e00-\u9fffA-Za-z0-9·/、]{1,16}"  # allows 、/ list separators, NOT clause ，
_SUBST_ARROW = re.compile(rf"({_TOK_MULTI})\s*(?:→|->|=>|＝>|►|=)\s*({_TOK_MULTI})")
_SUBST_CHANGE = re.compile(
    rf"(?:把|将)?\s*({_TOK_MULTI})\s*(?:统一|一律|都|一概)?\s*"
    rf"(?:改|换|说|叫|称呼?|写|表述)\s*(?:成|为|作)\s*({_TOK_MULTI})")
_SUBST_REPLACE = re.compile(rf"用\s*({_TOK_MULTI})\s*(?:来)?(?:代替|替代|替换|代|取代)\s*({_TOK_MULTI})")


def _clean_tok(s: str) -> str:
    s = s.strip(" 　，,、。.：:；;")
    s = re.sub(r"^(?:把|将|请|对|的|和|跟|与|让)+", "", s)
    s = re.sub(r"(?:统一|一律|一概|都|也|要|请|得|须|需|必须|改|换|说)+$", "", s)
    return s.strip(" 　，,、。.：:；;")


def extract_subst(sp: str) -> list[tuple[str, str]]:
    """Pull (forbidden, preferred) word-substitution rules out of a sys_prompt."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rx, reversed_ in ((_SUBST_ARROW, False), (_SUBST_CHANGE, False), (_SUBST_REPLACE, True)):
        for m in rx.finditer(sp):
            a, b = m.group(1), m.group(2)
            forb_raw, pref = (b, a) if reversed_ else (a, b)
            pref = _clean_tok(pref)
            for token in re.split(r"[、/，,]", forb_raw):
                token = _clean_tok(token)
                if token and pref and token != pref and token not in seen and len(token) <= 10:
                    seen.add(token)
                    out.append((token, pref))
    return out


def directive_compliance(turns: list[dict]) -> dict:
    rules = {name: {"applicable": 0, "violations": 0, "examples": []} for name, _, _ in NAMED_RULES}
    subst: dict = {}
    n_with_sp = 0
    for t in turns:
        sp = sys_prompt(t)
        if not sp.strip():
            continue
        n_with_sp += 1
        reply = t.get("reply") or ""
        scorable = bool(reply.strip())
        for name, det, vio in NAMED_RULES:
            if det(sp):
                rules[name]["applicable"] += 1
                if scorable and vio(reply, sp):
                    rules[name]["violations"] += 1
                    rules[name]["examples"].append(t)
        for forb, pref in extract_subst(sp):
            key = f"{forb}→{pref}"
            s = subst.setdefault(key, {"applicable": 0, "violations": 0, "examples": []})
            s["applicable"] += 1
            if scorable and forb in reply:
                s["violations"] += 1
                s["examples"].append(t)
    return {"rules": rules, "subst": subst, "n_with_sp": n_with_sp, "n": len(turns)}


def _comp_cell(stats: dict, key: str, named: bool) -> str:
    s = stats["rules"].get(key) if named else stats["subst"].get(key)
    if not s or s["applicable"] == 0:
        return "—"
    ap, vi = s["applicable"], s["violations"]
    return f"{ap} / {vi} ({pct(1 - vi / ap)})"


def compliance_block(before: list[dict], after: list[dict]) -> list[str]:
    cb, ca = directive_compliance(before), directive_compliance(after)
    out = [f"- 含自定义 system_prompt 的样本：前 {cb['n_with_sp']}/{cb['n']}、后 {ca['n_with_sp']}/{ca['n']}",
           "- 遵循率 = 1 − 违例/适用；**适用**=该侧 sys_prompt 里检出此规则的样本数。"
           "替换类规则由 sys_prompt 自动抽取（X→Y / 把X说成Y / 用Y代替X）。\n"]
    named = [name for name, _, _ in NAMED_RULES]
    subst_keys = sorted(set(cb["subst"]) | set(ca["subst"]))
    body = ["| 指令规则 | 前 适用n/违例(遵循率) | 后 适用n/违例(遵循率) |", "|---|---|---|"]
    rows = 0
    for name in named:
        if cb["rules"][name]["applicable"] or ca["rules"][name]["applicable"]:
            rows += 1
            body.append(f"| {name} | {_comp_cell(cb, name, True)} | {_comp_cell(ca, name, True)} |")
    for key in subst_keys:
        rows += 1
        body.append(f"| 替换 {key} | {_comp_cell(cb, key, False)} | {_comp_cell(ca, key, False)} |")
    if not rows:
        out.append("_两侧样本均未检出可正则核对的 system_prompt 指令（无 sys_prompt 或规则未覆盖）。"
                   "**模型仍须人工抽查 sys_prompt**，不得断言『指令被正确执行』。_")
        return out
    out += body
    # violation examples (for §3.1 evidence)
    evs = []
    for side, st in (("前", cb), ("后", ca)):
        for name in named:
            for t in st["rules"][name]["examples"][:3]:
                evs.append((side, name, t))
        for key in st["subst"]:
            for t in st["subst"][key]["examples"][:3]:
                evs.append((side, f"替换 {key}", t))
    if evs:
        out.append("")
        out.append("**违例样例（遵循率 <100% 即说明指令未被完全执行；可并入 §3.1 严重违规取证）：**\n")
        for side, rule, t in evs[:12]:
            snip = (t.get("reply") or "").strip().replace("\n", " ")[:80]
            cust = last_msg(t).strip().replace("\n", " ")[:24]
            out.append(f"- [{side}] 规则「{rule}」· agent=`{t.get('agent')}`"
                       f" · 客户:「{cust}」· 回复:「{snip}」")
    else:
        out.append("")
        out.append("_检出的可核对指令均未发现违例（注意：仅覆盖正则可识别的规则，非全部指令）。_")
    return out


# ---------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--out", default="prompt_eval_report.md",
                    help="final report path (default: ./prompt_eval_report.md in cwd)")
    ap.add_argument("--pairs", default="eval_pairs.md",
                    help="intermediate file with ALL pairs for scoring (not the final report)")
    ap.add_argument("--sample", type=int, default=30, help="max pairs per side to present for scoring")
    ap.add_argument("--server", default=None, help="which server this eval is for (e.g. Brain / weilike)")
    ap.add_argument("--upgrade", default=None, help="upgrade anchor (time / prompt_id cluster)")
    ap.add_argument("--before-raw", type=int, default=None, help="raw turn count before per-agent sampling")
    ap.add_argument("--after-raw", type=int, default=None, help="raw turn count before per-agent sampling")
    ap.add_argument("--keep-fake", action="store_true",
                    help="do NOT filter 假提问/非客户提问 (agent opening boilerplate, media-only, system notices)")
    args = ap.parse_args()

    before_all, after_all = load(args.before), load(args.after)
    if args.keep_fake:
        before, after = before_all, after_all
        drop_b, drop_a = [], []
    else:
        before, drop_b = partition_scorable(before_all)
        after, drop_a = partition_scorable(after_all)
    qb, qa = quant(before), quant(after)
    wb, wa = window(before), window(after)
    bsel, asel = select_pairs(before, args.sample), select_pairs(after, args.sample)

    # ---- intermediate pairs file (model reads this to score + pick typicals)
    P = ["# 中间文件 · 待评分问答对（不要并入最终报告）\n"]
    for line in RUBRIC.splitlines():
        P.append("> " + line)
    P.append("")
    P += pairs_block("升级前", "B", bsel)
    P += pairs_block("升级后", "A", asel)
    P.append("")
    P += score_table(bsel, asel)
    with open(args.pairs, "w", encoding="utf-8") as f:
        f.write("\n".join(P))

    # ---- final report
    L = ["# 提示词升级前后 · AI 回复质量对比\n"]

    L.append("## 0. 评测元数据\n")
    L.append(f"- 服务器: **{args.server or '未指定（建议补：评的是哪台 AI）'}**")
    L.append(f"- 升级锚点: **{args.upgrade or '未指定（建议补：升级时间 / prompt_id 簇）'}**")
    L.append(f"- 升级前窗口: `{wb['start']} → {wb['end']}`（约 {wb['hours']}h）")
    L.append(f"- 升级后窗口: `{wa['start']} → {wa['end']}`（约 {wa['hours']}h）")
    rawb = args.before_raw if args.before_raw is not None else "未提供"
    rawa = args.after_raw if args.after_raw is not None else "未提供"
    L.append(f"- 原始 turn 数(采样前): 前 {rawb} / 后 {rawa}；"
             f"评分采样: 前 {len(bsel)} / 后 {len(asel)}（每侧按 agent 轮转抽样）")
    L.append(f"- 来源: 前 `{qb['by_source']}` / 后 `{qa['by_source']}`")
    if args.keep_fake:
        L.append("- 假提问过滤: **已禁用**（--keep-fake）；非客户提问可能污染 relevance，请人工剔除。")
    else:
        def _reason_counts(drops):
            c = Counter(r for _, r in drops)
            return "、".join(f"{r}×{n}" for r, n in c.most_common()) or "无"
        L.append(f"- 假提问过滤（非客户提问，已从全部统计与评分中剔除）: "
                 f"前剔除 {len(drop_b)} 条（{_reason_counts(drop_b)}）、"
                 f"后剔除 {len(drop_a)} 条（{_reason_counts(drop_a)}）")
        ex = (drop_b[:3] + drop_a[:3])
        for t, r in ex:
            snip = last_msg(t).strip().replace("\n", " ")[:50]
            L.append(f"  - 剔除例 · {r} · 「{snip}」")
    L.append("")

    L.append("## 1. 量化指标（客观，自动统计）\n")
    L.append("| 指标 | 升级前 | 升级后 | 变化 |")
    L.append("|---|---|---|---|")
    L.append(f"| 样本数 | {qb['n']} | {qa['n']} | |")
    lb, la = qb["len"], qa["len"]
    L.append(f"| 回复字数 均值 | {lb['mean']} | {la['mean']} | {arrow(lb['mean'], la['mean'])} |")
    L.append(f"| 回复字数 中位 | {lb['median']} | {la['median']} | {arrow(lb['median'], la['median'])} |")
    L.append(f"| 回复字数 p25/p75（四分位距） | {lb['p25']}/{lb['p75']} | {la['p25']}/{la['p75']} | |")
    L.append(f"| 回复字数 p90 | {lb['p90']} | {la['p90']} | {arrow(lb['p90'], la['p90'])} |")
    L.append(f"| 回复字数 min/max/σ | {lb['min']}/{lb['max']}/{lb['sd']} | {la['min']}/{la['max']}/{la['sd']} | |")
    L.append(f"| 重复回复率 | {pct(qb['dup']['rate'])} | {pct(qa['dup']['rate'])} | {arrow_pp(qb['dup']['rate'], qa['dup']['rate'])} |")
    L.append(f"| 含表情/贴纸率 | {pct(qb['emoji'])} | {pct(qa['emoji'])} | {arrow_pp(qb['emoji'], qa['emoji'])} |")
    L.append(f"| 含具体金额/比例率 | {pct(qb['money'])} | {pct(qa['money'])} | {arrow_pp(qb['money'], qa['money'])} |")
    L.append("")
    hb, nb = len_hist(before)
    ha, na = len_hist(after)
    L.append("**回复字数分布直方图**（暴露双峰：开场白 ~100 字 vs 试镜/引导照片等短回复；均值会掩盖）\n")
    L.append("| 字数桶 | 升级前 n(占比) | 升级后 n(占比) |")
    L.append("|---|---|---|")
    for lab, cb, ca in zip(LEN_BUCKET_LABELS, hb, ha):
        L.append(f"| {lab} | {cb} ({pct(cb / nb) if nb else '-'}) | {ca} ({pct(ca / na) if na else '-'}) |")
    L.append("")
    L.append(f"- 最高频重复回复：前 {qb['dup']['top_n']}×、后 {qa['dup']['top_n']}×（同一句被重复发的次数）")
    L.append(f"- 升级前 agent 分布: `{qb['by_agent']}`")
    L.append(f"- 升级后 agent 分布: `{qa['by_agent']}`")
    L.append(f"- 升级前 stage 分布: `{qb['by_stage']}`")
    L.append(f"- 升级后 stage 分布: `{qa['by_stage']}`")
    L.append(f"- 模型分布: 前 `{qb['by_model']}` / 后 `{qa['by_model']}`\n")

    L.append("## 1b. 同 agent 客观对比（仅两侧都有的 agent；规避配比混杂）\n")
    L += matched_agent_objective(before, after)
    L.append("")

    L.append("## 1c. 自定义 system_prompt 指令遵循率（正则可核对子集，自动统计）\n")
    L.append("> 给『遵循率』而非一句『被正确执行』。仅覆盖可正则核对的规则（不发表情、开头不加客套词、"
             "开头不自我介绍、词语替换）；其余指令模型须人工抽查，且**不得**断言全部正确执行。\n")
    L += compliance_block(before, after)
    L.append("")

    L.append("## 2. 质量评分（由运行本 skill 的模型直接打分）\n")
    L.append("> 评分依据（读中间文件 `" + args.pairs + "`：先填逐条评分表，再取每侧均值填下表）：\n")
    for line in RUBRIC.splitlines():
        L.append("> " + line)
    L.append("")
    L.append(f"> 样本量：升级前 {len(bsel)} 条 / 升级后 {len(asel)} 条（整数打分，<~0.3 差异勿过度解读）。\n")
    L.append("| 维度 | 升级前 | 升级后 | 变化 |")
    L.append("|---|---|---|---|")
    for d in DIMS:
        L.append(f"| {DIM_CN[d]} | _填_ | _填_ | _填_ |")
    L.append("")
    L.append("**同 agent 配对（避免混杂；以此为主要判断依据）**\n")
    L += matched_agent_rubric_scaffold(before, after)
    L.append("\n**结论**：_由模型填写：整体 + 配对后 overall 升/降、哪些维度退化/改善、"
             "最可能原因（结合 §1/§1b 客观指标、§1c 指令遵循率、典型样例与注意事项）。"
             "涉及 system_prompt 指令时，**引用 §1c 的遵循率**，不要写『指令被正确执行』。_\n")

    L.append("## 3. 样例分类（好 / 坏分开，按严重程度排序）\n")
    L.append("> 由运行本 skill 的模型填写：从中间文件 `" + args.pairs + "` 中挑出**有代表性**的样例，"
             "分到下面三类。**每类内部按严重 / 显著程度从高到低排序**，先列最严重的。"
             "展示原文片段 + 一句说明；不要罗列全部问答对。\n")
    L.append("> 顺序固定为：先 **3.1 严重违规**（最优先）→ 再 **3.2 退化点**（变差）→ "
             "最后 **3.3 改善点**（变好）。每类无内容时写「无」。\n")

    L.append("### 3.1 严重违规（最优先列出；按严重度降序）\n")
    L.append("> 浮夸 / 违规的收益承诺、明显答非所问、违反经纪人硬性指令（参见 §1c 指令遵循率的违例样例："
             "`不要发表情` 仍发表情 / 贴纸、开头仍自我介绍、叫错老师名字 / 未做词语替换）、重复刷屏、"
             "有害或不当内容等。标注发生在**升级前还是升级后**、命中哪条 agent，并给严重度（高 / 中）。"
             "最严重的排最前。\n")
    L.append("| # | 严重度 | 发生侧(前/后) | 编号 | 违规类型 | 原文片段 | 说明（为何算严重违规） |")
    L.append("|---|---|---|---|---|---|---|")
    L.append("| 1 | 高 | _前/后_ | _Bx/Ay_ | _类型_ | _原文_ | _说明_ |")
    L.append("")
    L.append("_若无严重违规，写：无。_\n")

    L.append("### 3.2 退化点（升级后变差；按严重度降序）\n")
    L.append("> 升级后相对升级前明显变差的对照样例，最严重的排最前。\n")
    L.append("| # | 严重度 | 维度 | 升级前 `Bx` 原文 | 升级后 `Ay` 原文 | 说明（哪里变差） |")
    L.append("|---|---|---|---|---|---|")
    L.append("| 1 | _高/中/低_ | _如 相关切题 / 合规_ | _原文片段_ | _原文片段_ | _说明_ |")
    L.append("")
    L.append("_若无退化，写：无。_\n")

    L.append("### 3.3 改善点（升级后变好；按显著度降序）\n")
    L.append("> 升级后相对升级前明显变好的对照样例，最显著的排最前。\n")
    L.append("| # | 显著度 | 维度 | 升级前 `Bx` 原文 | 升级后 `Ay` 原文 | 说明（哪里变好） |")
    L.append("|---|---|---|---|---|---|")
    L.append("| 1 | _高/中/低_ | _如 主动引导 / 信息完整_ | _原文片段_ | _原文片段_ | _说明_ |")
    L.append("")
    L.append("_若无明显改善，写：无。_\n")

    L.append("## 4. 注意事项（自动生成 + 人工补充）\n")
    L += auto_caveats(qb, qa, wb, wa)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[report]      {args.out}", file=sys.stderr)
    print(f"[pairs(中间)] {args.pairs}", file=sys.stderr)
    print("\n".join(L))


if __name__ == "__main__":
    main()
