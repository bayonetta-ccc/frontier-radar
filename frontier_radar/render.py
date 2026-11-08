"""报告渲染：Markdown（可归档/分享）与 HTML（本地 Web UI）。

设计取舍：
* 默认层只回答四个问题——变了什么、为什么与我相关、能确认到什么程度、下一步看什么；
* 展开层才给原文摘录、独立来源、相反证据、版本历史与覆盖缺口；
* 事实、口径、推断、观点、传闻使用固定颜色与标签，不混写成同一个确定结论。
"""

from __future__ import annotations

import html
import json
from typing import Any, Iterable

from .models import (
    CHANGE_LABELS,
    INFO_LAYER_LABELS,
    Change,
    ChangeKind,
    Digest,
    InfoLayer,
)
from .text import confidence_label

KIND_ORDER = ["falsify", "shift", "weaken", "strengthen", "new"]

INFO_LAYER_CSS = {
    "fact": "layer-fact",
    "company_claim": "layer-claim",
    "inference": "layer-inference",
    "opinion": "layer-opinion",
    "rumor": "layer-rumor",
    "version": "layer-version",
}

STATUS_LABELS = {
    "proposed": "待确认",
    "active": "追踪中",
    "weakened": "已削弱",
    "falsified": "已证伪",
    "retired": "已停用",
    "rolled_back": "已回滚",
}

TIMELINE_TYPE_LABELS = {
    "actual_event": "实际事件",
    "official_plan": "官方计划",
    "prediction": "观点/预测",
    "inference": "系统推断",
}

# 标准类判断借用同一阶段刻度，但用“规范成熟度”的语言表达
STANDARD_STAGE_LABELS = {
    0: "研究阶段",
    1: "研究阶段",
    2: "草案阶段",
    3: "草案阶段",
    4: "公开征求意见",
    5: "发布实施",
}


def stage_label_for(kind: str, level: int, default: str) -> str:
    if kind == "standard" and level in STANDARD_STAGE_LABELS:
        return STANDARD_STAGE_LABELS[level]
    return default

FEEDBACK_VERDICTS = [
    ("useful", "有用"),
    ("irrelevant", "无关"),
    ("fact_error", "事实有误"),
    ("weak_evidence", "证据不足"),
    ("wrong_change", "变化分类有误"),
]

# 渲染期的信号索引：run_id -> signal_id -> 详情，避免在 HTML 每张卡片里重复查找 timeline
_SIGNAL_INDEX: dict[str, dict[str, dict[str, Any]]] = {}


def register_signals(run_id: str, records: list[dict[str, Any]]) -> None:
    _SIGNAL_INDEX[run_id] = {str(item.get("signal_id", "")): item for item in records}


def _signal_record(digest: Digest, signal_id: str) -> dict[str, Any]:
    record = _SIGNAL_INDEX.get(digest.run_id, {}).get(signal_id)
    if record:
        return record
    for node in digest.timeline:
        if node.get("signal_id") == signal_id:
            return node
    for item in digest.not_pushed:
        if item.get("signal_id") == signal_id:
            return item
    return {}


# --------------------------------------------------------------------- 通用


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def _delta(value: float) -> str:
    if abs(value) < 0.005:
        return "±0"
    return f"{value:+.2f}"


def _layer_badge(layer: str | InfoLayer) -> str:
    key = layer.value if isinstance(layer, InfoLayer) else str(layer)
    label = INFO_LAYER_LABELS.get(InfoLayer(key)) if key in {e.value for e in InfoLayer} else key
    return f'<span class="tag {INFO_LAYER_CSS.get(key, "layer-version")}">{_esc(label or key)}</span>'


def _change_badge(kind: ChangeKind | str) -> str:
    value = kind.value if isinstance(kind, ChangeKind) else str(kind)
    label = CHANGE_LABELS.get(ChangeKind(value), value) if value in {e.value for e in ChangeKind} else value
    return f'<span class="badge badge-{_esc(value)}">{_esc(label)}</span>'


def _conf_bar(value: float) -> str:
    width = max(2, min(100, round(value * 100)))
    return (
        f'<span class="conf"><span class="conf-track"><span class="conf-fill" '
        f'style="width:{width}%"></span></span><span class="conf-num">{value:.2f}</span></span>'
    )


def _short_date(value: str) -> str:
    return str(value)[:10]


def _source_name(digest: Digest, source_id: str) -> str:
    for source in digest.coverage.get("sources", []):
        if source.get("source_id") == source_id:
            return str(source.get("name", source_id))
    return source_id


# --------------------------------------------------------------------- Markdown


def render_markdown(digest: Digest) -> str:
    summary = digest.summary
    rel = digest.relevance
    changes = digest.changes
    lines: list[str] = []
    lines.append(f"# 前沿雷达 · {_date_range(digest)}")
    lines.append("")
    lines.append(
        f"> **{summary.get('mode_label', '')}** ｜ 主题：{digest.profile.get('name', '')} "
        f"｜ 身份：{rel.get('role', '')} ｜ 决策：{rel.get('decision', '')}"
    )
    lines.append("")
    lines.append(f"**一句话结论**：{summary.get('headline', '')}")
    lines.append("")
    lines.append(
        "| 信号 | 去重后 | 有效 | 重复 | 噪声 | 已写入 | 待确认 | 未解决 |\n"
        "|---|---|---|---|---|---|---|---|\n"
        f"| {summary.get('signal_total', 0)} | {summary.get('signal_unique', 0)} | "
        f"{summary.get('signal_effective', 0)} | {summary.get('duplicate_count', 0)} | "
        f"{summary.get('noise_count', 0)} | {summary.get('change_applied', 0)} | "
        f"{summary.get('change_pending', 0)} | {summary.get('unresolved_count', 0)} |"
    )
    lines.append("")

    if summary.get("change_pending"):
        lines.append("## ⚠️ 需要你确认的修改")
        lines.append("")
        for change in digest.changes:
            if not change.requires_confirmation:
                continue
            lines.append(
                f"- **{CHANGE_LABELS[change.kind]}** `{change.claim_id}`：{change.claim_statement}"
            )
            lines.append(f"  - 理由：{change.why_changed}")
        lines.append("")

    if digest.mode == "baseline":
        lines.append("## 初始基线与待验证问题")
        lines.append("")
        lines.append("首次追踪不产生“变化”，而是先建立可复核的判断基线；以下判断都已标记为**待用户确认**。")
        lines.append("")

    if digest.changes:
        lines.append("## 本期变化")
        lines.append("")
        for index, change in enumerate(digest.changes, 1):
            lines.extend(_markdown_change(digest, change, index))
    elif digest.mode != "baseline":
        lines.append("## 本期无需更新")
        lines.append("")
        lines.append("当前关键判断未发生变化。没有足够重要的新证据，因此不推送“看起来像更新”的内容。")
        lines.append("")

    if digest.timeline:
        lines.append("## 关键时间线")
        lines.append("")
        for node in digest.timeline[:12]:
            label = TIMELINE_TYPE_LABELS.get(node.get("type", ""), node.get("type", ""))
            lines.append(f"- `{node['date']}` **[{label}]** {node['title']}（{node.get('stage', '')}）")
        lines.append("")

    lines.append("## 与我相关")
    lines.append("")
    watch_items = list(rel.get("watch", [])[:3])
    watch_items += [f"{item}（待确认）" for item in rel.get("watch_pending", [])[:2]]
    lines.append(f"- **我应该关注**：{'；'.join(watch_items) or '暂无'}")
    lines.append(f"- **暂时不必关注**：{'；'.join(rel.get('ignore', [])[:2]) or '暂无（没有已被证伪的判断）'}")
    for action in rel.get("actions", [])[:3]:
        lines.append(f"- **下一步**：{action['action']}（{action['why']}）")
    if rel.get("reassess_when"):
        lines.append(f"- **出现以下信号时需要重新评估**：{'；'.join(rel['reassess_when'][:4])}")
    lines.append("")

    if digest.not_pushed:
        lines.append("## 本期没有推送的内容")
        lines.append("")
        for item in digest.not_pushed[:8]:
            lines.append(f"- {item['title']} — {item['reason']}")
        lines.append("")

    coverage = digest.coverage
    lines.append("## 来源覆盖与限制")
    lines.append("")
    lines.append(
        f"- 采集模式：`{coverage.get('mode', '')}`"
        + ("（合成演示数据）" if coverage.get("synthetic") else "")
        + f"；可用来源 {coverage.get('sources_ok', 0)}/{coverage.get('sources_total', 0)}"
    )
    lines.append(
        f"- 时间范围：{_short_date(coverage.get('window', {}).get('period_start', ''))} → "
        f"{_short_date(coverage.get('window', {}).get('period_end', ''))}"
    )
    for gap in coverage.get("gaps", [])[:6]:
        lines.append(f"- 覆盖缺口：{gap}")
    lines.append("")

    lines.append("## 验证指标（本期快照）")
    lines.append("")
    metrics = digest.metrics
    lines.append(
        f"- 重复率 {_pct(metrics.get('duplicate_rate', 0))} ｜ 噪声率 {_pct(metrics.get('noise_rate', 0))} "
        f"｜ 变化来源独立组 {metrics.get('changed_claim_source_groups', 0)}"
    )
    if changes:
        lines.append(
            f"- 变化卡链接完整率 {_pct(metrics.get('changed_claim_link_ok', 0))} ｜ "
            f"原文摘录完整率 {_pct(metrics.get('changed_claim_quote_ok', 0))} ｜ "
            f"含相反证据的判断 {metrics.get('claims_with_both_sides', 0)}/{metrics.get('claims_total', 0)}"
        )
    else:
        lines.append("- 本期没有进入判断的变化，链接/摘录完整率不适用。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "本报告由 Frontier Radar 生成。判断是可回滚的当前状态，不是永久事实；"
        "未确认的高风险更新不会被自动写入。"
    )
    lines.append("")
    return "\n".join(lines)


def _markdown_change(digest: Digest, change: Change, index: int) -> list[str]:
    lines = [
        f"### {index}. [{CHANGE_LABELS[change.kind]}] {change.claim_statement}",
        "",
        f"- **影响分**：{round(change.score * 100)}/100 ｜ **置信度**：{change.confidence_before:.2f} → "
        f"{change.confidence_after:.2f}（{_delta(change.confidence_delta)}） ｜ "
        f"**阶段**：{stage_label_for(_claim_kind(digest, change.claim_id), change.stage_before, f'S{change.stage_before}')} → "
        f"{stage_label_for(_claim_kind(digest, change.claim_id), change.stage_after, f'S{change.stage_after}')}"
        + (" ｜ **待确认**" if change.requires_confirmation else ""),
        f"- **为什么变化**：{change.why_changed}",
        f"- **为什么重要**：{change.why_matters}",
        f"- **与我何干**：{change.relevance_to_user}",
        f"- **下一步观察**：{'；'.join(change.next_steps[:3])}",
    ]
    if change.headline:
        lines.insert(1, f"\n> {change.headline}\n")
    if change.open_questions:
        cleaned = [str(q).rstrip("。；; ") for q in change.open_questions[:3]]
        lines.append(f"- **仍不能确认**：{'；'.join(cleaned)}")
    for signal in change.supporting[:3]:
        lines.append(
            f"- **支持证据** `{signal.info_layer.value}`：{_signal_title(digest, signal.signal_id)}"
        )
    for signal in change.contradicting[:3]:
        lines.append(
            f"- **相反证据** `{signal.info_layer.value}`：{_signal_title(digest, signal.signal_id)}"
        )
    lines.append("")
    return lines


def _claim_kind(digest: Digest, claim_id: str) -> str:
    for claim in digest.claims:
        if claim.get("claim_id") == claim_id:
            return str(claim.get("claim_kind", ""))
    return ""


def _date_range(digest: Digest) -> str:
    return f"{_short_date(digest.period_start)} → {_short_date(digest.period_end)}"


# --------------------------------------------------------------------- HTML


CSS = """
:root{
  --bg:#f6f5f1; --panel:#fffdf8; --ink:#1e2329; --muted:#5f6a72; --line:#e4e0d6;
  --accent:#0f6b5c; --accent-soft:#e6f0ec; --amber:#a35a00; --amber-soft:#fdf1dd;
  --red:#9d2c2c; --red-soft:#f8e7e5; --blue:#274a78; --blue-soft:#e8eef7;
  --violet:#5a4377; --violet-soft:#f0eaf6; --radius:14px;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --serif:"Iowan Old Style","Palatino Linotype","Songti SC","Noto Serif SC",Georgia,serif;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);line-height:1.65;font-size:15px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1120px;margin:0 auto;padding:0 22px}
header.top{background:#14211f;color:#eef3ef;padding:14px 0;position:sticky;top:0;z-index:20}
header.top .wrap{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
.brand{font-family:var(--serif);font-size:19px;letter-spacing:.5px;font-weight:600;color:#fff}
.brand small{display:block;font-family:var(--sans);font-size:11px;color:#9fb5ad;letter-spacing:1.4px}
nav.main{display:flex;gap:18px;font-size:13.5px;margin-left:auto;flex-wrap:wrap}
nav.main a{color:#cfded8;padding:4px 0;border-bottom:2px solid transparent}
nav.main a.active,nav.main a:hover{color:#fff;border-color:#5fb3a0;text-decoration:none}
main{padding:28px 0 64px}
.hero{display:grid;grid-template-columns:1.5fr 1fr;gap:22px;align-items:start;margin-bottom:22px}
@media(max-width:820px){.hero{grid-template-columns:1fr}}
h1{font-family:var(--serif);font-size:30px;line-height:1.25;margin:6px 0 8px;font-weight:600}
h2{font-family:var(--serif);font-size:21px;margin:34px 0 12px;font-weight:600;display:flex;align-items:center;gap:10px}
h2 .idx{font-family:var(--sans);font-size:12px;color:var(--muted);letter-spacing:1px}
h3{font-size:16px;margin:0 0 6px;line-height:1.5}
p{margin:0 0 10px}
.lede{color:var(--muted);font-size:14px}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 0}
.pill{display:inline-flex;align-items:center;gap:6px;background:var(--panel);border:1px solid var(--line);
  border-radius:999px;padding:3px 11px;font-size:12px;color:var(--muted)}
.pill b{color:var(--ink);font-weight:600}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px;
  box-shadow:0 1px 0 rgba(30,35,41,.03)}
.card+.card{margin-top:14px}
.summary{border-left:4px solid var(--accent)}
.summary .headline{font-family:var(--serif);font-size:18px;margin:2px 0 10px}
.grid{display:grid;gap:14px}
.grid.stats{grid-template-columns:repeat(4,1fr)}
.grid.two{grid-template-columns:1fr 1fr}
@media(max-width:860px){.grid.stats{grid-template-columns:repeat(2,1fr)}.grid.two{grid-template-columns:1fr}}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.stat .n{font-family:var(--serif);font-size:26px;line-height:1.1}
.stat .k{font-size:11.5px;color:var(--muted);letter-spacing:.4px}
.stat.warn .n{color:var(--amber)} .stat.bad .n{color:var(--red)} .stat.good .n{color:var(--accent)}
.badge{display:inline-block;font-size:11.5px;font-weight:700;letter-spacing:.4px;border-radius:6px;padding:2px 8px}
.badge-strengthen{background:var(--accent-soft);color:var(--accent)}
.badge-weaken{background:var(--amber-soft);color:var(--amber)}
.badge-falsify{background:var(--red-soft);color:var(--red)}
.badge-shift{background:var(--blue-soft);color:var(--blue)}
.badge-new{background:var(--violet-soft);color:var(--violet)}
.badge-no_change{background:#eef0f2;color:#5b6469}
.tag{display:inline-block;font-size:11px;border-radius:5px;padding:1px 7px;margin-right:5px;border:1px solid transparent;white-space:nowrap}
.layer-fact{background:var(--accent-soft);color:var(--accent);border-color:#cfe3da}
.layer-claim{background:var(--blue-soft);color:var(--blue);border-color:#d3ddec}
.layer-inference{background:var(--violet-soft);color:var(--violet);border-color:#e0d6ec}
.layer-opinion{background:#f1efe9;color:#6a6152;border-color:#e3ded2}
.layer-rumor{background:var(--red-soft);color:var(--red);border-color:#eed4d1}
.layer-version{background:#eceff1;color:#546066}
.change{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:0;overflow:hidden;margin-bottom:14px}
.change .body{padding:18px 20px}
.change.pending{border-color:#e7c98f}
.change .topline{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.change .statement{font-family:var(--serif);font-size:18.5px;line-height:1.45;margin:2px 0 12px}
.change .headline{color:var(--muted);font-size:13.5px;margin:-6px 0 12px}
.conf{display:inline-flex;align-items:center;gap:7px;font-variant-numeric:tabular-nums}
.conf-track{width:84px;height:5px;background:#e7e3d9;border-radius:99px;overflow:hidden;display:inline-block}
.conf-fill{display:block;height:100%;background:var(--accent)}
.conf-num{font-size:12.5px;color:var(--muted)}
.kv{display:grid;grid-template-columns:96px 1fr;gap:8px 14px;font-size:13.8px;margin:10px 0 0}
.kv dt{color:var(--muted);font-size:12.5px;padding-top:2px}
.kv dd{margin:0}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.btn{display:inline-block;border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:8px;
  padding:6px 12px;font-size:12.5px;cursor:pointer;font-family:var(--sans)}
.btn:hover{border-color:#bfc7c4;text-decoration:none}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.danger{color:var(--red);border-color:#e3c3bf}
.btn.small{padding:3px 9px;font-size:11.5px}
details{border-top:1px dashed var(--line);margin-top:14px;padding-top:10px}
details>summary{cursor:pointer;font-size:13px;color:var(--accent);font-weight:600;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"＋ ";font-weight:400}
details[open]>summary::before{content:"− "}
.details-inner{padding-top:12px}
.evidence{background:#faf8f2;border:1px solid #ece7db;border-radius:10px;padding:12px 14px;margin:8px 0}
.evidence .etitle{font-weight:600;font-size:13.8px;margin-bottom:4px}
.evidence .quote{font-family:var(--serif);font-size:13.5px;color:#3a4147;border-left:3px solid #d8d2c4;
  padding-left:10px;margin:8px 0;white-space:pre-wrap}
.evidence .src{font-size:12px;color:var(--muted);display:flex;gap:10px;flex-wrap:wrap}
.timeline{list-style:none;margin:0;padding:0}
.timeline li{position:relative;padding:0 0 18px 26px;border-left:2px solid #e3ded2;margin-left:6px}
.timeline li:last-child{border-left-color:transparent}
.timeline li::before{content:"";position:absolute;left:-7px;top:5px;width:12px;height:12px;border-radius:50%;
  background:#fff;border:2.5px solid var(--accent)}
.timeline li.t-official_plan::before{border-color:var(--blue)}
.timeline li.t-prediction::before{border-color:var(--amber)}
.timeline li.t-inference::before{border-color:var(--violet)}
.timeline .when{font-size:11.5px;color:var(--muted);letter-spacing:.4px;font-variant-numeric:tabular-nums}
.timeline .what{font-size:14px;margin-top:1px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin-bottom:8px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
.legend .l1 i{background:var(--accent)} .legend .l2 i{background:var(--blue)}
.legend .l3 i{background:var(--amber)} .legend .l4 i{background:var(--violet)}
.claim-row{display:grid;grid-template-columns:64px 1fr 110px 120px;gap:12px;align-items:center;
  padding:12px 4px;border-bottom:1px solid var(--line);font-size:13.8px}
.claim-row:last-child{border-bottom:0}
.claim-row .sid{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:var(--muted)}
@media(max-width:760px){.claim-row{grid-template-columns:1fr;gap:4px}}
.notpushed li{font-size:13.2px;color:#4d565c;margin-bottom:6px}
.gaps li{font-size:13.2px;color:#4d565c}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
@media(max-width:760px){.metrics{grid-template-columns:1fr 1fr}}
.metric{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:#fbfaf6}
.metric .v{font-family:var(--serif);font-size:19px}
.metric .l{font-size:11.5px;color:var(--muted)}
table.tbl{width:100%;border-collapse:collapse;font-size:13px}
table.tbl th{text-align:left;color:var(--muted);font-weight:600;font-size:11.5px;border-bottom:1px solid var(--line);padding:6px 8px}
table.tbl td{border-bottom:1px solid #efebe2;padding:7px 8px;vertical-align:top}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.muted{color:var(--muted)}
.empty{color:var(--muted);font-size:13.5px;padding:8px 0}
.notice{background:var(--amber-soft);border:1px solid #ecd7ae;color:#6d4508;border-radius:10px;padding:10px 14px;font-size:13px;margin:14px 0}
.footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:6px 0 18px}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:12.5px;color:var(--muted)}
.chip b{color:var(--ink)}
textarea{width:100%;border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-family:var(--sans);font-size:12.5px;background:#fff}
form.inline{display:inline}
details.form>summary{color:var(--muted);font-weight:400;font-size:12px;border-top:0;margin-top:0}
"""


def page(title: str, body: str, *, active: str = "", base: str = "", footer: str = "") -> str:
    nav = [
        ("runs", "总览", "index.html"),
        ("claims", "判断库", "claims.html"),
        ("about", "说明", "about.html"),
    ]
    nav_html = "".join(
        f'<a href="{base}{href}" class="{"active" if key == active else ""}">{label}</a>'
        for key, label, href in nav
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<header class="top"><div class="wrap">
  <div class="brand">前沿雷达 <small>FRONTIER RADAR · EVIDENCE FIRST</small></div>
  <nav class="main">{nav_html}</nav>
</div></header>
<main><div class="wrap">
{body}
<div class="footer">{footer or "Frontier Radar · 判断是可回滚的当前状态，不是永久事实。"}</div>
</div></main>
</body>
</html>
"""


def render_digest_body(digest: Digest, *, base: str = "") -> str:
    s = digest.summary
    rel = digest.relevance
    parts: list[str] = []
    parts.append(
        f'<div class="meta"><span class="pill">主题 <b>{_esc(digest.profile.get("name", ""))}</b></span>'
        f'<span class="pill">身份 <b>{_esc(rel.get("role", "—"))}</b></span>'
        f'<span class="pill">决策窗口 <b>{_esc(rel.get("horizon", "—"))}</b></span>'
        f'<span class="pill">模式 <b>{_esc(s.get("mode_label", ""))}</b></span>'
        f'<span class="pill">运行 <b class="mono">{_esc(digest.run_id)}</b></span></div>'
    )
    parts.append(
        f'<div class="card summary" style="margin-top:18px">'
        f'<div class="muted" style="font-size:12px;letter-spacing:1px">一句话结论</div>'
        f'<div class="headline">{_esc(s.get("headline", ""))}</div>'
        f'<div class="grid stats">'
        f'{_stat(s.get("signal_effective", 0), "有效信号", "")}'
        f'{_stat(s.get("change_applied", 0) + s.get("change_pending", 0), "判断更新", "good" if s.get("change_applied") else "")}'
        f'{_stat(s.get("change_pending", 0), "待你确认", "warn" if s.get("change_pending") else "")}'
        f'{_stat(s.get("duplicate_count", 0) + s.get("noise_count", 0), "重复/噪声拦截", "")}'
        f'</div></div>'
    )
    if digest.coverage.get("synthetic"):
        parts.append(
            '<div class="notice"><b>演示模式</b>：本期使用合成 fixture 数据（example.com 占位链接），'
            "用于复现证据链与判断更新逻辑，不代表真实世界的最新进展。</div>"
        )
    pending = [c for c in digest.changes if c.requires_confirmation]
    if pending:
        parts.append(
            f'<h2><span class="idx">01</span>需要你确认的修改</h2>'
            '<div class="card" style="border-left:4px solid var(--amber)">'
            '<p class="lede">以下修改涉及高风险判断或独立来源不足，系统不会自动写入当前判断。请核对证据后确认或驳回。</p>'
            + "".join(_pending_row(c) for c in pending)
            + "</div>"
        )

    if digest.mode == "baseline":
        parts.append(
            '<h2><span class="idx">02</span>初始基线（待你确认）</h2>'
            '<div class="card"><p class="lede">首次追踪先建立可复核的判断基线。以下判断全部标记为“待确认”，'
            "确认前不会参与自动迭代。你可以确认、修改表述，或直接删除。</p>"
            + "".join(_baseline_claim_row(claim) for claim in digest.claims)
            + "</div>"
        )
    parts.append(f'<h2><span class="idx">{"03" if digest.mode == "baseline" else "02"}</span>本期变化</h2>')
    if digest.changes:
        for index, change in enumerate(digest.changes, 1):
            parts.append(render_change_card(digest, change, index))
    else:
        parts.append(
            '<div class="card"><p><b>本期无需更新。</b>当前关键判断未发生变化，仍需观察的指标见“与我相关”。</p>'
            '<p class="lede">“敢不推送”是去噪能力的一部分：热度不等于实质变化。</p></div>'
        )

    if digest.claims:
        parts.append(f'<h2><span class="idx">{"04" if digest.timeline else "03"}</span>当前判断状态</h2>')
        parts.append('<div class="card">')
        for claim in digest.claims:
            parts.append(_claim_snapshot_row(claim))
        parts.append('<p style="margin-top:12px"><a href="claims.html">查看完整状态库与版本历史 →</a></p></div>')

    if digest.timeline:
        parts.append('<h2><span class="idx">03</span>关键时间线</h2><div class="card">')
        parts.append(
            '<div class="legend"><span class="l1"><i></i>实际事件</span>'
            '<span class="l2"><i></i>官方计划</span><span class="l3"><i></i>观点/预测</span>'
            '<span class="l4"><i></i>系统推断</span></div>'
        )
        parts.append('<ul class="timeline">')
        for node in digest.timeline:
            label = TIMELINE_TYPE_LABELS.get(node.get("type", ""), node.get("type", ""))
            parts.append(
                f'<li class="t-{_esc(node.get("type", ""))}">'
                f'<div class="when">{_esc(node.get("date", ""))} · {_esc(label)} · {_esc(node.get("stage", ""))}</div>'
                f'<div class="what">{_esc(node.get("title", ""))}</div></li>'
            )
        parts.append("</ul></div>")

    parts.append('<h2><span class="idx">04</span>与我相关</h2><div class="card">')
    parts.append('<div class="grid two">')
    parts.append(
        '<div><h3>我应该关注</h3><ul class="gaps">'
        + "".join(f"<li>{_esc(item)}</li>" for item in rel.get("watch", [])[:4])
        + (
            "".join(
                f'<li>{_esc(item)} <span class="tag layer-claim">待确认</span></li>'
                for item in rel.get("watch_pending", [])[:3]
            )
            if rel.get("watch_pending")
            else ""
        )
        + '</ul><h3 style="margin-top:14px">暂时不必关注</h3><ul class="gaps">'
        + ("".join(f"<li>{_esc(item)}</li>" for item in rel.get("ignore", [])[:3]) or '<li class="muted">暂无（没有被证伪的判断）</li>')
        + "</ul></div>"
    )
    parts.append('<div><h3>下一步可以做什么</h3><ul class="gaps">')
    for action in rel.get("actions", [])[:4]:
        parts.append(f"<li>{_esc(action.get('action', ''))}<br><span class=\"muted\" style=\"font-size:12px\">{_esc(action.get('why', ''))}</span></li>")
    if not rel.get("actions"):
        parts.append('<li class="muted">本期没有需要立即行动的变化。</li>')
    parts.append("</ul>")
    if rel.get("reassess_when"):
        parts.append('<h3 style="margin-top:14px">出现以下信号需要重新评估</h3><ul class="gaps">')
        parts.append("".join(f"<li>{_esc(item)}</li>" for item in rel["reassess_when"][:4]))
        parts.append("</ul>")
    parts.append("</div></div></div>")

    if digest.not_pushed:
        parts.append('<h2><span class="idx">05</span>本期没有推送的内容</h2><div class="card"><ul class="notpushed">')
        for item in digest.not_pushed[:12]:
            parts.append(
                f'<li><span class="badge badge-no_change">{_esc(item.get("info_layer", ""))}</span> '
                f'{_esc(item.get("title", ""))} <span class="muted">— {_esc(item.get("reason", ""))}</span></li>'
            )
        parts.append("</ul></div>")

    coverage = digest.coverage
    parts.append('<h2><span class="idx">06</span>来源覆盖与限制</h2><div class="card">')
    parts.append(
        f'<p class="lede">采集模式 <b>{_esc(coverage.get("mode", ""))}</b>'
        + ("（合成演示数据）" if coverage.get("synthetic") else "")
        + f'；可用来源 {coverage.get("sources_ok", 0)}/{coverage.get("sources_total", 0)}；'
        f'时间范围 {_esc(coverage.get("window", {}).get("period_start", ""))} → '
        f'{_esc(coverage.get("window", {}).get("period_end", ""))}</p>'
    )
    parts.append('<table class="tbl"><thead><tr><th>来源</th><th>类型</th><th>权威性</th><th>状态</th><th>本期信号</th><th>说明</th></tr></thead><tbody>')
    for source in coverage.get("sources", []):
        parts.append(
            f'<tr><td>{_esc(source.get("name", ""))}</td><td>{_esc(source.get("kind", ""))}</td>'
            f'<td>{_esc(source.get("authority", ""))}</td><td>{_esc(source.get("status", ""))}</td>'
            f'<td>{source.get("signal_count", 0)}</td><td class="muted">{_esc(source.get("note", ""))}</td></tr>'
        )
    parts.append("</tbody></table>")
    if coverage.get("gaps"):
        parts.append('<h3 style="margin-top:16px">覆盖缺口</h3><ul class="gaps">')
        parts.append("".join(f"<li>{_esc(gap)}</li>" for gap in coverage["gaps"][:8]))
        parts.append("</ul>")
    if coverage.get("single_source_claims"):
        parts.append('<h3 style="margin-top:16px">仍只有单一来源的判断</h3><ul class="gaps">')
        parts.append("".join(f"<li>{_esc(item)}</li>" for item in coverage["single_source_claims"]))
        parts.append("</ul>")
    parts.append("</div>")

    metrics = digest.metrics
    parts.append('<h2><span class="idx">07</span>验证指标（本期快照）</h2>')
    metrics = digest.metrics
    no_changes = int(metrics.get("changes_with_confidence_delta", 0) or 0) == 0 and not digest.changes
    parts.append('<div class="card"><div class="metrics">')
    for label, value in [
        ("重复率", _pct(metrics.get("duplicate_rate", 0))),
        ("噪声率", _pct(metrics.get("noise_rate", 0))),
        ("变化来源独立组", str(metrics.get("changed_claim_source_groups", 0))),
        ("链接完整率", "—" if no_changes else _pct(metrics.get("changed_claim_link_ok", 0))),
        ("摘录完整率", "—" if no_changes else _pct(metrics.get("changed_claim_quote_ok", 0))),
        ("含相反证据判断", f"{metrics.get('claims_with_both_sides', 0)}/{metrics.get('claims_total', 0)}"),
    ]:
        parts.append(f'<div class="metric"><div class="v">{_esc(value)}</div><div class="l">{_esc(label)}</div></div>')
    parts.append(f'</div><p class="lede" style="margin-top:10px">{_esc(metrics.get("note", ""))}'
                 + (" 本期没有进入判断的变化，链接/摘录完整率不适用。" if no_changes else "")
                 + "</p></div>")
    return "".join(parts)


def _stat(value: Any, label: str, cls: str = "") -> str:
    return f'<div class="stat {cls}"><div class="n">{_esc(value)}</div><div class="k">{_esc(label)}</div></div>'


def _baseline_claim_row(claim: dict) -> str:
    status = str(claim.get("status", ""))
    pending = bool(claim.get("pending_review")) or status == "proposed"
    return (
        f'<div style="display:flex;gap:12px;align-items:flex-start;margin-top:12px;flex-wrap:wrap">'
        f'<div style="flex:1;min-width:240px">'
        f'<div>{_esc(claim.get("statement", ""))}</div>'
        f'<div class="muted" style="font-size:12px;margin-top:4px">'
        f'置信度 {float(claim.get("confidence", 0)):.2f} · 阶段 S{claim.get("stage_level", 0)} · '
        f'待验证：{_esc((claim.get("open_questions") or ["—"])[0])}</div></div>'
        f'<div class="actions" style="margin:0">'
        f'<form class="inline" method="post" action="/api/claims/confirm"><input type="hidden" name="claim_id" value="{_esc(claim.get("claim_id", ""))}">'
        f'<input type="hidden" name="redirect" value="index.html"><button class="btn primary" type="submit">确认</button></form>'
        f'<form class="inline" method="post" action="/api/claims/retire"><input type="hidden" name="claim_id" value="{_esc(claim.get("claim_id", ""))}">'
        f'<input type="hidden" name="redirect" value="index.html"><button class="btn small" type="submit">删除</button></form>'
        f"</div></div>"
        + ("" if pending else '<div class="muted" style="font-size:12px">已确认</div>')
    )


def _claim_snapshot_row(claim: dict) -> str:
    status = str(claim.get("status", ""))
    return (
        f'<div class="claim-row"><span class="sid">{_esc(claim.get("claim_id", "").split("_")[-1])}</span>'
        f'<span>{_esc(claim.get("statement", ""))}'
        + (' <span class="tag layer-rumor">待确认</span>' if claim.get("pending_review") else "")
        + f'</span><span>{_conf_bar(float(claim.get("confidence", 0)))}</span>'
        f'<span><span class="tag layer-version">{_esc(STATUS_LABELS.get(status, status))}</span></span></div>'
    )


def _pending_row(change: Change) -> str:
    return (
        f'<div style="display:flex;gap:12px;align-items:flex-start;margin-top:10px;flex-wrap:wrap">'
        f'<div style="flex:1;min-width:240px"><b>{_esc(change.claim_statement)}</b>'
        f'<div class="muted" style="font-size:12.5px">{_esc(change.why_changed)}</div></div>'
        f'<div class="actions" style="margin:0">'
        f'<form class="inline" method="post" action="/api/claims/confirm"><input type="hidden" name="claim_id" value="{_esc(change.claim_id)}">'
        f'<input type="hidden" name="redirect" value="claims.html"><button class="btn primary" type="submit">确认写入</button></form>'
        f'<form class="inline" method="post" action="/api/claims/reject"><input type="hidden" name="claim_id" value="{_esc(change.claim_id)}">'
        f'<input type="hidden" name="redirect" value="claims.html"><button class="btn small" type="submit">驳回</button></form>'
        f"</div></div>"
    )


def render_change_card(digest: Digest, change: Change, index: int) -> str:
    delta = change.confidence_delta
    arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
    pending_class = " pending" if change.requires_confirmation else ""
    parts = [
        f'<article class="change{pending_class}" id="{_esc(change.change_id)}">',
        '<div class="body">',
        '<div class="topline">',
        f'<span class="muted mono">{index:02d}</span>',
        _change_badge(change.kind),
        f'<span class="muted" style="font-size:12px">置信度 {change.confidence_before:.2f} {arrow} {change.confidence_after:.2f}</span>',
        f'<span class="muted" style="font-size:12px">阶段 S{change.stage_before} → S{change.stage_after}</span>',
        f'<span class="pill" style="margin-left:auto">影响 <b>{round(change.score * 100)}</b>/100</span>',
    ]
    if change.high_risk:
        parts.append('<span class="badge badge-falsify">高风险判断</span>')
    if change.requires_confirmation:
        parts.append('<span class="badge badge-weaken">待确认</span>')
    parts.append("</div>")
    parts.append(f'<div class="statement">{_esc(change.claim_statement)}</div>')
    if change.headline:
        parts.append(f'<div class="headline">{_esc(change.headline)}</div>')
    parts.extend(
        [
            '<dl class="kv">',
            f'<dt>为什么变化</dt><dd>{_esc(change.why_changed)}</dd>',
            f'<dt>为什么重要</dt><dd>{_esc(change.why_matters)}</dd>',
            f'<dt>与我何干</dt><dd>{_esc(change.relevance_to_user)}</dd>',
            f'<dt>下一步观察</dt><dd>{"；".join(_esc(s) for s in change.next_steps[:3]) or "等待下一次独立来源确认。"}</dd>',
            "</dl>",
        ]
    )
    parts.append('<details><summary>展开证据链：原文摘录、独立来源、相反证据与版本</summary><div class="details-inner">')
    if change.supporting:
        parts.append('<div class="muted" style="font-size:12px;letter-spacing:.6px">支持证据</div>')
        for signal in change.supporting[:4]:
            parts.append(_evidence_block(digest, signal))
    if change.contradicting:
        parts.append('<div class="muted" style="font-size:12px;letter-spacing:.6px;margin-top:10px">相反证据</div>')
        for signal in change.contradicting[:4]:
            parts.append(_evidence_block(digest, signal))
    if change.open_questions:
        parts.append('<div class="muted" style="font-size:12px;letter-spacing:.6px;margin-top:12px">仍不能确认 / 待验证</div><ul class="gaps">')
        parts.append(
            "".join(f"<li>{_esc(str(q).rstrip('。；; '))}</li>" for q in change.open_questions[:4])
        )
        parts.append("</ul>")
    parts.append(
        f'<p class="muted" style="font-size:12px;margin-top:12px">判断 <span class="mono">{_esc(change.claim_id)}</span>'
        f' · 写入版本 v{change.version_after} · 变化编号 <span class="mono">{_esc(change.change_id)}</span></p>'
    )
    parts.append("</div></details>")
    parts.append('<div class="actions">')
    parts.append(f'<a class="btn small" href="{_esc("claims.html")}#{_esc(change.claim_id)}">查看判断历史</a>')
    for verdict, label in FEEDBACK_VERDICTS:
        parts.append(
            f'<form class="inline" method="post" action="/api/feedback">'
            f'<input type="hidden" name="claim_id" value="{_esc(change.claim_id)}">'
            f'<input type="hidden" name="run_id" value="{_esc(digest.run_id)}">'
            f'<input type="hidden" name="task_id" value="{_esc(digest.task_id)}">'
            f'<input type="hidden" name="verdict" value="{_esc(verdict)}">'
            f'<button class="btn small" type="submit">{_esc(label)}</button></form>'
        )
    parts.append("</div></div></article>")
    return "".join(parts)


def _evidence_block(digest: Digest, signal) -> str:
    record = _signal_record(digest, signal.signal_id)
    source = _source_name(digest, str(record.get("source_id", "")))
    link = str(record.get("url", ""))
    excerpt = str(record.get("excerpt", ""))
    extra = " ".join(
        part
        for part in [
            f"类别 {record.get('category', '')}" if record.get("category") else "",
            f"新颖性 {float(record.get('novelty', 0)):.2f}" if record.get("novelty") else "",
            "同源多报" if record.get("duplicate_of") else "",
        ]
        if part
    )
    return (
        f'<div class="evidence"><div class="etitle">{_layer_badge(signal.info_layer)} '
        f'{_esc(str(record.get("title", signal.signal_id)))}</div>'
        f'<div class="quote">{_esc(excerpt) or "（来源未提供可直接引用的原文摘录）"}</div>'
        f'<div class="src"><span>{_esc(source)}</span><span>{_esc(signal.reliability)}可信度</span>'
        f'<span>S{signal.stage}</span><span>{_esc(extra)}</span>'
        + (f'<a href="{_esc(link)}" target="_blank" rel="noopener">原始链接 ↗</a>' if link else "")
        + "</div></div>"
    )


def _signal_source(digest: Digest, signal_id: str) -> str:
    record = _signal_record(digest, signal_id)
    return str(record.get("source_id", ""))


def _signal_link(digest: Digest, signal_id: str) -> str:
    return str(_signal_record(digest, signal_id).get("url", ""))


def _signal_excerpt(digest: Digest, signal_id: str) -> str:
    return str(_signal_record(digest, signal_id).get("excerpt", ""))


def _signal_title(digest: Digest, signal_id: str) -> str:
    return str(_signal_record(digest, signal_id).get("title", signal_id))


# --------------------------------------------------------------------- 页面


def render_run_page(digest: Digest) -> str:
    body = [
        f'<h1>前沿雷达 · {_esc(_date_range(digest))}</h1>',
        f'<p class="lede">{_esc(digest.profile.get("description", ""))}</p>',
        render_digest_body(digest),
    ]
    return page(f"前沿雷达 · {_date_range(digest)}", "".join(body), active="runs")


def render_dashboard(runs: list[dict], task: dict | None, claims: list[dict]) -> str:
    task = task or {}
    role = task.get("role", "")
    body = [
        "<h1>你的前沿情报雷达</h1>",
        f'<p class="lede">围绕“{_esc(task.get("topic", "—"))}”持续追踪可说明范围的高可信来源；'
        "只推送关键变化，并说明证据、边界与下一步验证什么。</p>",
    ]
    if task:
        body.append(
            '<div class="meta">'
            f'<span class="pill">身份 <b>{_esc(role)}</b></span>'
            f'<span class="pill">目的 <b>{_esc(task.get("purpose", ""))}</b></span>'
            f'<span class="pill">决策 <b>{_esc(task.get("decision", ""))}</b></span>'
            f'<span class="pill">窗口 <b>{_esc(task.get("horizon", ""))}</b></span></div>'
        )
    if not runs:
        body.append(
            '<div class="card" style="margin-top:20px"><h3>还没有运行记录</h3>'
            '<p class="lede">先执行一次基线运行：<span class="mono">python -m frontier_radar run --demo --now 2026-07-28</span></p></div>'
        )
        return page("前沿雷达 · 总览", "".join(body), active="runs")
    body.append('<h2><span class="idx">01</span>运行记录</h2>')
    body.append('<div class="card"><table class="tbl"><thead><tr><th>时间</th><th>模式</th><th>结论</th><th>信号</th><th>应用 / 待确认</th><th></th></tr></thead><tbody>')
    for run in runs:
        summary = run.get("summary", {})
        body.append(
            f'<tr><td class="mono">{_esc(_short_date(run.get("period_end", "")))}</td>'
            f'<td>{_esc(summary.get("mode_label", run.get("mode", "")))}</td>'
            f'<td>{_esc(summary.get("headline", ""))}</td>'
            f'<td>{summary.get("signal_effective", 0)}</td>'
            f'<td>{summary.get("change_applied", 0)} / {summary.get("change_pending", 0)}</td>'
            f'<td><a href="{_esc(run.get("run_id", ""))}.html">查看报告 →</a></td></tr>'
        )
    body.append("</tbody></table></div>")
    pending_claims = [claim for claim in claims if claim.get("pending_review")]
    if pending_claims:
        body.append('<div class="notice"><b>有修改等待确认。</b>高风险判断或独立来源不足的更新不会自动写入，请到判断库核对。</div>')
    if claims:
        body.append('<h2><span class="idx">02</span>当前判断（部分）</h2><div class="card">')
        for claim in claims[:6]:
            body.append(_claim_row(claim))
        body.append(f'<p style="margin-top:12px"><a href="claims.html">查看全部判断与历史版本 →</a></p></div>')
    return page("前沿雷达 · 总览", "".join(body), active="runs")


def _claim_row(claim: dict) -> str:
    status = str(claim.get("status", ""))
    label = STATUS_LABELS.get(status, status)
    pending = (
        '<span class="tag layer-rumor">待确认</span>'
        if claim.get("pending_review")
        else ""
    )
    return (
        f'<div class="claim-row"><span class="sid">{_esc(claim.get("claim_id", "").split("_")[-1])}</span>'
        f'<span>{_esc(claim.get("statement", ""))}</span>'
        f'<span>{_conf_bar(float(claim.get("confidence", 0)))}</span>'
        f'<span><span class="tag layer-version">{_esc(label)}</span>'
        f"{pending}</span></div>"
    )


def render_claims_page(claims: list[dict], task: dict | None) -> str:
    task = task or {}
    body = [
        "<h1>判断状态库</h1>",
        '<p class="lede">这里保存的不是文章，而是“当前相信什么、置信度多少、依据哪些证据、何时被修改”。'
        "每条判断都可以回滚到历史版本。</p>",
    ]
    if not claims:
        body.append('<div class="card"><p class="lede">还没有判断。先运行一次基线。</p></div>')
        return page("判断库 · 前沿雷达", "".join(body), active="claims")
    body.append(f'<div class="toolbar"><span class="chip">共 <b>{len(claims)}</b> 条判断</span>'
                f'<span class="chip">追踪中 <b>{sum(1 for c in claims if c.get("status") in {"active", "weakened"})}</b></span>'
                f'<span class="chip">待确认 <b>{sum(1 for c in claims if c.get("pending_review"))}</b></span></div>')
    for claim in claims:
        body.append(_claim_card(claim))
    return page("判断库 · 前沿雷达", "".join(body), active="claims")


def _claim_card(claim: dict) -> str:
    status = str(claim.get("status", ""))
    body = [
        f'<div class="card" id="{_esc(claim.get("claim_id", ""))}" style="margin-bottom:14px">',
        f'<div class="topline" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">'
        f'<span class="mono muted">{_esc(claim.get("claim_id", ""))}</span>'
        f'<span class="tag layer-version">{_esc(STATUS_LABELS.get(status, status))}</span>'
        f'<span class="muted" style="font-size:12px">阶段 S{claim.get("stage_level", 0)}</span>'
        f'<span class="muted" style="font-size:12px">更新于 {_esc(_short_date(claim.get("updated_at", "")))}</span>'
        + ('<span class="tag layer-rumor">待确认修改</span>' if claim.get("pending_review") else "")
        + "</div>",
        f'<h3 style="font-family:var(--serif);font-size:17.5px;margin-top:8px">{_esc(claim.get("statement", ""))}</h3>',
        f'<div style="margin:6px 0 10px">{_conf_bar(float(claim.get("confidence", 0)))} '
        f'<span class="muted" style="font-size:12px">置信度（{_esc(confidence_label(float(claim.get("confidence", 0))))}） · '
        f'来源可信度 {_esc(claim.get("source_reliability", ""))} · 影响判断置信度 {_esc(claim.get("impact_confidence", ""))}</span></div>',
    ]
    if claim.get("pending_review"):
        body.append('<p class="lede">存在待确认的修改，确认前不会写入当前判断。</p>')
    body.append('<details><summary>展开：待验证问题、观察指标与版本历史</summary><div class="details-inner">')
    if claim.get("open_questions"):
        body.append('<div class="muted" style="font-size:12px">待验证问题</div><ul class="gaps">')
        body.append("".join(f"<li>{_esc(q)}</li>" for q in claim["open_questions"][:5]))
        body.append("</ul>")
    if claim.get("watch_indicators"):
        body.append('<div class="muted" style="font-size:12px">下一步观察指标</div><ul class="gaps">')
        body.append("".join(f"<li>{_esc(q)}</li>" for q in claim["watch_indicators"][:5]))
        body.append("</ul>")
    if claim.get("next_verification"):
        body.append(f'<p class="lede">验证方式：{_esc(claim["next_verification"])}</p>')
    versions = claim.get("versions", [])
    if versions:
        body.append('<div class="muted" style="font-size:12px;margin-top:8px">版本历史</div>')
        body.append('<table class="tbl"><thead><tr><th>版本</th><th>时间</th><th>类型</th><th>置信度</th><th>理由</th></tr></thead><tbody>')
        for version in versions:
            verify = "<span class=\"tag layer-claim\">待确认</span>" if version.get("user_correction") == "pending" else ""
            body.append(
                f'<tr><td class="mono">v{version.get("version")}</td>'
                f'<td class="mono">{_esc(_short_date(version.get("changed_at", "")))}</td>'
                f'<td>{_esc(version.get("change_kind", ""))} {verify}</td>'
                f'<td>{float(version.get("confidence", 0)):.2f}</td>'
                f'<td class="muted">{_esc(version.get("reason", ""))}'
                f'<div style="margin-top:4px">{_esc(version.get("statement", ""))}</div></td></tr>'
            )
        body.append("</tbody></table>")
    body.append("</div></details>")
    if claim.get("pending_review"):
        body.append(
            f'<div class="actions"><form class="inline" method="post" action="/api/claims/confirm">'
            f'<input type="hidden" name="claim_id" value="{_esc(claim.get("claim_id", ""))}">'
            f'<button class="btn primary" type="submit">确认修改</button></form>'
            f'<form class="inline" method="post" action="/api/claims/reject">'
            f'<input type="hidden" name="claim_id" value="{_esc(claim.get("claim_id", ""))}">'
            f'<button class="btn danger" type="submit">驳回修改</button></form></div>'
        )
    if claim.get("versions") and len(claim["versions"]) > 1:
        target = claim["versions"][-1]
        body.append(
            f'<div class="actions"><form class="inline" method="post" action="/api/claims/rollback">'
            f'<input type="hidden" name="claim_id" value="{_esc(claim.get("claim_id", ""))}">'
            f'<input type="hidden" name="target_version" value="{target.get("version")}">'
            f'<button class="btn small" type="submit">回滚到最早版本 v{target.get("version")}</button></form></div>'
        )
    body.append("</div>")
    return "".join(body)


def render_about_page(task: dict | None, profile_slug: str) -> str:
    body = f"""
<h1>这个工具在做什么，不做什么</h1>
<p class="lede">个人前沿情报雷达围绕一个具体目标，持续监测可说明范围的高可信来源，
把新信息转化为有证据、有边界、事实与判断分离、可追溯、可回滚的判断更新。</p>
<h2><span class="idx">01</span>五个问题</h2>
<div class="card"><ol>
<li>最近发生了什么？</li>
<li>哪些是有实质意义的信号，哪些只是营销噪声？</li>
<li>谁在推动这些变化？</li>
<li>结论的证据在哪里，哪些仍不能确认？</li>
<li>这些变化与我的职业、目标或当前决策有什么关系？</li>
</ol></div>
<h2><span class="idx">02</span>证据分级与判断状态</h2>
<div class="card">
<p>{_layer_badge("fact")} 已确认事实 · {_layer_badge("company_claim")} 公司/机构口径 ·
{_layer_badge("inference")} 分析推断 · {_layer_badge("opinion")} 行业观点 · {_layer_badge("rumor")} 未证实传闻</p>
<p class="lede">系统分别评价：来源可信度、事实置信度、影响判断置信度。三者不能互相替代：
公司“宣布”建试点线，可以确认它作出了宣布，但不能确认技术已成熟。</p>
<p>变化类型：{_change_badge("new")} 新增 {_change_badge("strengthen")} 增强 {_change_badge("weaken")} 削弱
{_change_badge("falsify")} 证伪 {_change_badge("shift")} 转向 {_change_badge("no_change")} 无实质变化</p>
</div>
<h2><span class="idx">03</span>边界</h2>
<div class="card"><ul class="gaps">
<li>不覆盖全网，只覆盖可说明的核心来源，并明确列出覆盖缺口。</li>
<li>不把热度当重要性，不把官方口径当独立验证。</li>
<li>证据不足时不给确定性行动建议；高风险判断的更新需要人工确认。</li>
<li>没有实质变化时明确输出“本期无需更新”，而不是制造更新。</li>
</ul></div>
<h2><span class="idx">04</span>数据与隐私</h2>
<div class="card"><p>全部数据保存在本地 SQLite（<span class="mono">data/radar.db</span>）与
<span class="mono">data/artifacts/</span>。默认离线 fixture 模式不联网；只有显式使用
<span class="mono">--live</span> 才会按来源 URL 拉取公开页面。不使用任何云端账号或密钥。</p>
<p class="lede">当前领域配置：<span class="mono">{_esc(profile_slug)}</span></p></div>
"""
    return page("说明 · 前沿雷达", body, active="about")


def render_claims_data(claims) -> list[dict]:
    return [
        {
            "claim_id": claim.claim_id,
            "statement": claim.statement,
            "status": claim.status.value,
            "confidence": claim.confidence,
            "source_reliability": claim.source_reliability,
            "impact_confidence": claim.impact_confidence,
            "stage_level": claim.stage_level,
            "high_risk": claim.high_risk,
            "pending_review": claim.pending_review,
            "open_questions": list(claim.open_questions),
            "watch_indicators": list(claim.watch_indicators),
            "next_verification": claim.next_verification,
            "updated_at": claim.updated_at,
            "versions": [],
        }
        for claim in claims
    ]


__all__ = [
    "render_markdown",
    "render_digest_body",
    "render_change_card",
    "render_run_page",
    "render_dashboard",
    "render_claims_page",
    "render_about_page",
    "render_claims_data",
    "page",
    "CSS",
]
