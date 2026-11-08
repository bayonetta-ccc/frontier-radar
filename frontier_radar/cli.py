"""命令行：初始化任务、运行追踪、审阅判断、采集、验证与导出。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .db import Database
from .engine import build_metrics, claim_state, list_claim_states, publish_site
from .fixtures import DEMO_RUNS, load_demo_task, run_demo
from .models import CHANGE_LABELS, Claim, ClaimStatus, Digest
from .pipeline import RadarPipeline, _record_dict, export_digest
from .profile import ProfileError, load_profile
from .render import FEEDBACK_VERDICTS
from .text import confidence_label, iso_now

DEFAULT_DB = "data/radar.db"
DEFAULT_PROFILE = "solid_state_battery"
VERDICT_KEYS = [key for key, _label in FEEDBACK_VERDICTS]


# --------------------------------------------------------------------- 输出


class Console:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def line(self, text: str = "") -> None:
        if self.enabled:
            print(text)

    def ok(self, text: str) -> None:
        self.line(f"[OK] {text}")

    def warn(self, text: str) -> None:
        self.line(f"[!] {text}")

    def title(self, text: str) -> None:
        self.line("")
        self.line(text)
        self.line("-" * min(64, max(12, len(text) * 2)))


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def _change_line(change, index: int) -> list[str]:
    delta = change.confidence_after - change.confidence_before
    flag = "  [待确认]" if change.requires_confirmation else ""
    return [
        f"{index:>2}. [{CHANGE_LABELS[change.kind]}] {change.claim_statement}{flag}",
        f"    置信度 {change.confidence_before:.2f} → {change.confidence_after:.2f}（{delta:+.2f}）"
        f" · 阶段 S{change.stage_before} → S{change.stage_after} · 影响 {round(change.score * 100)}/100",
        f"    为什么：{change.why_changed}",
        f"    与我何干：{change.relevance_to_user}",
        f"    下一步：{'；'.join(change.next_steps[:2])}",
    ]


def print_digest(digest: Digest, console: Console) -> None:
    s = digest.summary
    console.title(f"前沿雷达 · {digest.period_start[:10]} → {digest.period_end[:10]} · {s.get('mode_label', '')}")
    console.line(f"结论：{s.get('headline', '')}")
    console.line(
        f"信号 {s.get('signal_total', 0)} 条（去重 {s.get('signal_unique', 0)}，有效 {s.get('signal_effective', 0)}）"
        f" · 拦截重复 {s.get('duplicate_count', 0)} · 噪声 {s.get('noise_count', 0)}"
        f" · 更新 {s.get('change_applied', 0)} · 待确认 {s.get('change_pending', 0)}"
        f" · 未解决 {s.get('unresolved_count', 0)}"
    )
    if digest.coverage.get("synthetic"):
        console.warn("本期使用合成演示数据（fixture），不代表真实世界的最新进展。")

    if digest.mode == "baseline" and digest.claims:
        console.line("")
        console.line("初始判断基线与待验证问题：")
        for claim in digest.claims:
            console.line(f"  · {claim['statement']}")
            console.line(
                f"    置信度 {claim['confidence']:.2f}（{confidence_label(float(claim['confidence']))}）"
                f" · 阶段 S{claim['stage_level']} · 状态 {claim['status']}（待确认）"
            )
            if claim.get("open_questions"):
                console.line(f"    待验证：{claim['open_questions'][0]}")
            if claim.get("watch_indicators"):
                console.line(f"    观察：{'；'.join(claim['watch_indicators'][:2])}")

    if digest.changes:
        console.line("")
        for index, change in enumerate(digest.changes, 1):
            for line in _change_line(change, index):
                console.line(line)
            console.line("")
    elif digest.mode != "baseline":
        console.line("")
        console.line("本期无需更新：当前关键判断未发生变化。")
        if digest.relevance.get("reassess_when"):
            console.line(f"仍需观察：{'；'.join(digest.relevance['reassess_when'][:3])}")

    if digest.not_pushed:
        console.line("")
        console.line(f"未推送 {len(digest.not_pushed)} 条（重复/噪声/低价值）：")
        for item in digest.not_pushed[:6]:
            console.line(f"  · {item['title']} — {item['reason']}")

    coverage = digest.coverage
    console.line("")
    console.line(
        f"来源：{coverage.get('sources_ok', 0)}/{coverage.get('sources_total', 0)} 可用"
        f" · 重复率 {_pct(digest.metrics.get('duplicate_rate', 0))}"
        f" · 噪声率 {_pct(digest.metrics.get('noise_rate', 0))}"
        f" · 变化来源独立组 {digest.metrics.get('changed_claim_source_groups', 0)}"
    )
    if digest.coverage.get("gaps"):
        console.line(f"覆盖缺口：{digest.coverage['gaps'][0]}")
    console.line(f"报告：data/artifacts/{digest.task_id}/{digest.run_id}/digest.md")


# --------------------------------------------------------------------- 上下文


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.data_dir = Path(args.data_dir)
        self.db_path = Path(args.db) if args.db else self.data_dir / "radar.db"
        self.profile_slug = args.profile
        self.fixtures_dir = Path(args.fixtures)
        self.console = Console(not args.quiet)
        self._profile = None

    @property
    def profile(self):
        if self._profile is None:
            self._profile = load_profile(self.profile_slug)
        return self._profile

    def db(self) -> Database:
        return Database(self.db_path)

    def pipeline(self, db: Database) -> RadarPipeline:
        return RadarPipeline(
            db,
            self.profile,
            fixtures_dir=self.fixtures_dir,
            artifact_root=self.data_dir / "artifacts",
        )

    def task_id(self, db: Database, explicit: str | None) -> str:
        if explicit:
            return explicit
        tasks = db.list_tasks()
        if not tasks:
            raise SystemExit("还没有任务。先运行 `python -m frontier_radar demo` 或 `task new --demo`。")
        return tasks[0]["task_id"]


# --------------------------------------------------------------------- 命令


def cmd_demo(args: argparse.Namespace, ctx: Context) -> int:
    output = run_demo(
        data_dir=ctx.data_dir,
        profile=ctx.profile_slug,
        fixtures_dir=ctx.fixtures_dir,
        reset=not args.keep,
        source_mode="offline",
    )
    print(output.summary)
    if args.confirm_after:
        db = Database(output.db_path)
        try:
            pipeline = ctx.pipeline(db)
            confirmed = 0
            for claim in db.list_claims(output.task_id):
                if claim.pending_review:
                    pipeline.engine.confirm(claim.claim_id, note="demo --confirm-after")
                    confirmed += 1
        finally:
            db.close()
        print(f"已确认 {confirmed} 条待确认修改（演示参数 --confirm-after）。")
    if not args.quiet:
        for result in output.runs:
            print()
            print_digest(result.digest, ctx.console)
    site = publish_site(output.db_path.parent, output.task_id)
    if site:
        ctx.console.ok(f"静态站点：{site}")
    return 0


def cmd_task_new(args: argparse.Namespace, ctx: Context) -> int:
    db = ctx.db()
    try:
        if args.demo:
            task = load_demo_task()
            task["task_id"] = args.id or task["task_id"]
        else:
            missing = [k for k in ("topic",) if not getattr(args, k, None)]
            if missing:
                raise SystemExit("至少需要 --topic；或用 --demo 使用演示任务。")
            task = {
                "task_id": args.id or "task_" + ctx.profile_slug,
                "topic": args.topic,
                "role": args.role,
                "purpose": args.purpose,
                "decision": args.decision,
                "constraints": args.constraints,
                "horizon": args.horizon,
                "decision_criteria": args.decision_criteria,
                "watchlist": [x for x in (args.watchlist or "").split(",") if x],
                "notes": args.notes,
                "profile_slug": ctx.profile_slug,
            }
        pipeline = ctx.pipeline(db)
        task_id = pipeline.create_task(task)
        ctx.console.ok(f"任务已创建：{task_id}")
        ctx.console.line(f"  主题：{task['topic']}")
        ctx.console.line(f"  决策：{task.get('decision') or '（未填写）'}")
        ctx.console.line(f"  约束：{task.get('constraints') or '（未填写）'}")
        ctx.console.line(f"  窗口：{task.get('horizon') or '（未填写）'}")
    finally:
        db.close()
    return 0


def cmd_task_list(args: argparse.Namespace, ctx: Context) -> int:
    db = ctx.db()
    try:
        tasks = db.list_tasks()
        if not tasks:
            ctx.console.line("（暂无任务）")
        for task in tasks:
            runs = db.list_runs(task["task_id"], limit=1)
            last = runs[0]["finished_at"][:10] if runs else "—"
            ctx.console.line(
                f"{task['task_id']}  {task['topic']}  · {task['role']}  · 最近运行 {last}"
            )
    finally:
        db.close()
    return 0


def cmd_task_show(args: argparse.Namespace, ctx: Context) -> int:
    db = ctx.db()
    try:
        task_id = ctx.task_id(db, args.id)
        task = db.get_task(task_id)
        if not task:
            raise SystemExit(f"任务不存在: {task_id}")
        for key in (
            "task_id", "topic", "role", "purpose", "decision", "constraints",
            "horizon", "decision_criteria", "notes", "profile_slug",
        ):
            ctx.console.line(f"{key:<18} {task.get(key, '')}")
        ctx.console.line(f"{'watchlist':<18} {'、'.join(task.get('watchlist', []))}")
        sources = db.list_sources(task_id)
        ctx.console.line(f"来源（{len(sources)}）：")
        for source in sources:
            state = "启用" if source["enabled"] else "停用"
            ctx.console.line(
                f"  · {source['source_id']:<24} {source['name']} [{source['kind']}/{source['authority']}] {state}"
            )
    finally:
        db.close()
    return 0


def cmd_run(args: argparse.Namespace, ctx: Context) -> int:
    db = ctx.db()
    try:
        task_id = ctx.task_id(db, args.task)
        pipeline = ctx.pipeline(db)
        result = pipeline.run(
            task_id,
            mode=args.mode,
            source_mode=args.source,
            now=args.now,
            period_days=args.period_days,
            auto_confirm=args.auto_confirm,
            confirm_after=args.confirm_after,
        )
        if not args.json:
            print_digest(result.digest, ctx.console)
        else:
            print(json.dumps(result.digest.to_dict(), ensure_ascii=False, indent=2))
        if args.artifact:
            paths = export_digest(
                result.digest,
                Path(args.artifact),
                [_record_dict(record) for record in result.signals],
            )
            ctx.console.ok(f"已导出：{paths}")
        site = publish_site(ctx.data_dir, task_id)
        if site and not args.json:
            ctx.console.line(f"静态站点：{site}/index.html")
        if result.digest.summary.get("change_pending"):
            ctx.console.warn("有修改等待确认：`python -m frontier_radar claims list --pending`")
        return 0
    finally:
        db.close()


def cmd_ingest(args: argparse.Namespace, ctx: Context) -> int:
    from .sources import LiveCollector

    db = ctx.db()
    try:
        task_id = ctx.task_id(db, args.task)
        sources = [s for s in ctx.profile.sources]
        collector = LiveCollector(
            ctx.profile,
            timeout=args.timeout,
            total_timeout=args.total_timeout,
        )
        result = collector.fetch(sources)
        ctx.console.line(f"采集模式 live：{len(result.signals)} 条信号")
        for access in result.access:
            ctx.console.line(
                f"  · {access.source_id:<24} {access.status:<16} 抓取 {access.fetched:>3} · {access.note or '—'}"
            )
        if result.errors:
            ctx.console.warn("部分来源不可达；运行时会作为覆盖缺口记录，不会阻断整次更新。")
        db.add_event(task_id, "ingest_check", {"sources": len(sources), "signals": len(result.signals)})
        if args.write_cache:
            cache = ctx.fixtures_dir / ctx.profile.slug / "signals" / "live-cache.json"
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps(
                    {
                        "demo": False,
                        "generated_at": iso_now(),
                        "signals": [s.__dict__ for s in result.signals],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            ctx.console.ok(f"写入缓存：{cache}（下一轮离线运行会读取）")
    finally:
        db.close()
    return 0


def _find_claim(db: Database, task_id: str, token: str) -> Claim:
    claim = db.get_claim(token)
    if claim:
        return claim
    token = token.lower()
    candidates = [
        c
        for c in db.list_claims(task_id)
        if c.claim_id.lower().endswith(token) or token in c.claim_id.lower() or token in c.statement.lower()
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SystemExit(f"找不到判断：{token}")
    raise SystemExit("匹配到多个判断，请用更精确的 claim_id：" + "、".join(c.claim_id for c in candidates))


def cmd_claims_list(args: argparse.Namespace, ctx: Context) -> int:
    db = ctx.db()
    try:
        task_id = ctx.task_id(db, args.task)
        states = list_claim_states(db, task_id)
        if args.pending:
            states = [s for s in states if s["pending_review"]]
        if not states:
            ctx.console.line("（没有符合条件的判断）")
            return 0
        for state in states:
            if state["pending_review"] or state["status"] == "proposed":
                status_text = f"{state['status']} · 待确认"
            else:
                status_text = state["status"]
            flag = " [待确认修改]" if state["pending_review"] else ""
            ctx.console.line("")
            ctx.console.line(f"{state['claim_id']}  {state['statement']}{flag}")
            ctx.console.line(
                f"  状态 {status_text} · 置信度 {state['confidence']:.2f}"
                f"（{confidence_label(state['confidence'])}）· 阶段 S{state['stage_level']}"
                f" · 来源可信度 {state['source_reliability']} · 影响置信度 {state['impact_confidence']}"
            )
            ctx.console.line(
                f"  支持 {len(state['supporting_signal_ids'])} · 相反 {len(state['contradicting_signal_ids'])}"
                f" · 版本 v{state['version']} · 更新 {state['updated_at'][:10]}"
            )
            if args.verbose:
                for question in state["open_questions"][:3]:
                    ctx.console.line(f"  待验证：{question}")
                for indicator in state["watch_indicators"][:3]:
                    ctx.console.line(f"  观察：{indicator}")
    finally:
        db.close()
    return 0


def cmd_claims_show(args: argparse.Namespace, ctx: Context) -> int:
    db = ctx.db()
    try:
        task_id = ctx.task_id(db, args.task)
        claim = _find_claim(db, task_id, args.id)
        state = claim_state(db, claim)
        ctx.console.line(f"{state['claim_id']}")
        ctx.console.line(state["statement"])
        ctx.console.line(
            f"状态 {state['status']} · 置信度 {state['confidence']:.2f} · 阶段 S{state['stage_level']}"
            f" · 高风险 {'是' if state['high_risk'] else '否'} · 待确认 {'是' if state['pending_review'] else '否'}"
        )
        ctx.console.line("")
        ctx.console.line("版本历史：")
        for version in state["versions"]:
            mark = " ← 待确认" if version["user_correction"] == "pending" else ""
            ctx.console.line(
                f"  v{version['version']:<2} {version['changed_at'][:10]} [{version['change_kind']}]"
                f" 置信度 {version['confidence']:.2f} 阶段 S{version['stage_level']}{mark}"
            )
            ctx.console.line(f"      {version['statement']}")
            ctx.console.line(f"      理由：{version['reason']}")
        evidence = db.evidence_for_claim(claim.claim_id)
        if evidence:
            ctx.console.line("")
            ctx.console.line("证据台账：")
            for item in evidence[-8:]:
                ctx.console.line(
                    f"  [{item['direction']}] {item['published_at'][:10]} {item['title'][:56]}"
                    f" · {item['weight']:.2f}"
                )
        if args.json:
            print(json.dumps(state, ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def cmd_claims_confirm(args: argparse.Namespace, ctx: Context) -> int:
    db = ctx.db()
    try:
        task_id = ctx.task_id(db, args.task)
        pipeline = ctx.pipeline(db)
        claim = _find_claim(db, task_id, args.id)
        updated = pipeline.engine.confirm(claim.claim_id, note=args.note or "")
        ctx.console.ok(f"已确认并写入当前判断：v{updated.version} · 置信度 {updated.confidence:.2f}")
    finally:
        db.close()
    return 0


def cmd_claims_reject(args: argparse.Namespace, ctx: Context) -> int:
    db = ctx.db()
    try:
        task_id = ctx.task_id(db, args.task)
        pipeline = ctx.pipeline(db)
        claim = _find_claim(db, task_id, args.id)
        pipeline.engine.reject(claim.claim_id, note=args.note or "")
        ctx.console.ok("已驳回待确认修改；当前判断保持不变，事件已记录。")
    finally:
        db.close()
    return 0


def cmd_claims_rollback(args: argparse.Namespace, ctx: Context) -> int:
    db = ctx.db()
    try:
        task_id = ctx.task_id(db, args.task)
        pipeline = ctx.pipeline(db)
        claim = _find_claim(db, task_id, args.id)
        updated = pipeline.engine.rollback(claim.claim_id, args.to, note=args.note or "")
        ctx.console.ok(f"已回滚到 v{args.to}，生成新版本 v{updated.version}（历史版本保留）。")
    finally:
        db.close()
    return 0


def cmd_claims_retire(args: argparse.Namespace, ctx: Context) -> int:
    db = ctx.db()
    try:
        task_id = ctx.task_id(db, args.task)
        claim = _find_claim(db, task_id, args.id)
        claim.status = ClaimStatus.RETIRED
        claim.updated_at = iso_now()
        db.upsert_claim(claim)
        db.add_event(task_id, "claim_retired", {"claim_id": claim.claim_id, "note": args.note or ""})
        ctx.console.ok(f"已停用判断 {claim.claim_id}（历史版本与证据保留）。")
    finally:
        db.close()
    return 0


def cmd_feedback(args: argparse.Namespace, ctx: Context) -> int:
    if args.verdict not in VERDICT_KEYS:
        raise SystemExit("verdict 必须是：" + "、".join(VERDICT_KEYS))
    db = ctx.db()
    try:
        task_id = ctx.task_id(db, args.task)
        if args.claim:
            claim = _find_claim(db, task_id, args.claim)
            claim_id = claim.claim_id
        else:
            claim_id = ""
        db.add_feedback(
            task_id,
            verdict=args.verdict,
            run_id=args.run or "",
            claim_id=claim_id,
            signal_id=args.signal or "",
            note=args.note or "",
        )
        db.add_event(
            task_id,
            "feedback",
            {"verdict": args.verdict, "claim_id": claim_id, "signal_id": args.signal or ""},
        )
        ctx.console.ok(f"已记录反馈：{args.verdict}。反馈会进入验证指标与后续纠错流程。")
    finally:
        db.close()
    return 0


def cmd_metrics(args: argparse.Namespace, ctx: Context) -> int:
    db = ctx.db()
    try:
        task_id = ctx.task_id(db, args.task)
        metrics = build_metrics(db, task_id)
        if args.json:
            print(json.dumps(metrics, ensure_ascii=False, indent=2))
            return 0
        ctx.console.title("验证指标")
        for section, values in metrics.items():
            if not isinstance(values, dict):
                continue
            ctx.console.line(f"[{section}]")
            for key, value in values.items():
                ctx.console.line(f"  {key:<34} {value}")
        return 0
    finally:
        db.close()


def cmd_serve(args: argparse.Namespace, ctx: Context) -> int:
    from .web import serve

    return serve(
        data_dir=ctx.data_dir,
        host=args.host,
        port=args.port,
        open_browser=args.open,
    )


def cmd_validate(args: argparse.Namespace, ctx: Context) -> int:
    from .validate import validate_profile

    report = validate_profile(ctx.profile)
    for line in report["lines"]:
        ctx.console.line(line)
    if report["errors"]:
        ctx.console.warn(f"发现 {len(report['errors'])} 个配置问题。")
        return 1
    ctx.console.ok("领域配置校验通过。")
    return 0


# --------------------------------------------------------------------- 解析器


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frontier-radar",
        description="个人前沿情报雷达：把新信息转化为有证据、有边界、可回滚的判断更新。",
    )
    parser.add_argument("--version", action="version", version=f"frontier-radar {__version__}")
    parser.add_argument("--data-dir", default="data", help="数据目录（默认 data/）")
    parser.add_argument("--db", default="", help="SQLite 路径（默认 <data-dir>/radar.db）")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help=f"领域配置（默认 {DEFAULT_PROFILE}）")
    parser.add_argument("--fixtures", default="config/fixtures", help="离线 fixture 目录")
    parser.add_argument("--quiet", action="store_true", help="只输出必要信息")

    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="重建并运行完整演示（baseline → 更新 → 静默）")
    demo.add_argument("--keep", action="store_true", help="保留已有数据，不重置 data/")
    demo.add_argument("--confirm-after", action="store_true", dest="confirm_after", help="演示结束后确认待确认修改")
    demo.set_defaults(func=cmd_demo)

    task = sub.add_parser("task", help="任务（主题 + 目标 + 决策 + 约束）")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_new = task_sub.add_parser("new", help="创建任务")
    task_new.add_argument("--demo", action="store_true", help="使用演示任务")
    task_new.add_argument("--id", default="", help="任务 id")
    task_new.add_argument("--topic", default="", help="关注主题")
    task_new.add_argument("--role", default="", help="身份与目的")
    task_new.add_argument("--purpose", default="", help="为什么关注")
    task_new.add_argument("--decision", default="", help="当前要做的判断")
    task_new.add_argument("--constraints", default="", help="关键约束")
    task_new.add_argument("--horizon", default="", help="决策时间范围")
    task_new.add_argument("--decision-criteria", default="", dest="decision_criteria")
    task_new.add_argument("--watchlist", default="", help="关注主体，逗号分隔")
    task_new.add_argument("--notes", default="")
    task_new.set_defaults(func=cmd_task_new)

    task_list = task_sub.add_parser("list", help="列出任务")
    task_list.set_defaults(func=cmd_task_list)

    task_show = task_sub.add_parser("show", help="查看任务与来源")
    task_show.add_argument("--id", default="")
    task_show.set_defaults(func=cmd_task_show)

    run = sub.add_parser("run", help="运行一次追踪（基线或更新）")
    run.add_argument("--task", default="")
    run.add_argument("--mode", choices=["auto", "baseline", "update"], default="auto")
    run.add_argument("--source", choices=["offline", "live"], default="offline")
    run.add_argument("--now", default="", help="以指定时间重放（ISO8601）")
    run.add_argument("--period-days", type=int, default=7, dest="period_days")
    run.add_argument("--auto-confirm", action="store_true", dest="auto_confirm", help="自动确认上一期待确认修改（仅演示）")
    run.add_argument("--confirm-after", action="store_true", dest="confirm_after", help="本期结束后确认待确认修改（仅演示）")
    run.add_argument("--json", action="store_true", help="输出完整 JSON")
    run.add_argument("--artifact", default="", help="额外导出目录")
    run.set_defaults(func=cmd_run)

    ingest = sub.add_parser("ingest", help="检查在线来源可达性（不写判断）")
    ingest.add_argument("--task", default="")
    ingest.add_argument("--timeout", type=int, default=12, help="单来源超时秒数")
    ingest.add_argument("--total-timeout", type=int, default=45, dest="total_timeout", help="整轮抓取总时限秒数")
    ingest.add_argument("--write-cache", action="store_true", dest="write_cache")
    ingest.set_defaults(func=cmd_ingest)

    claims = sub.add_parser("claims", help="判断状态库")
    claims_sub = claims.add_subparsers(dest="claims_command", required=True)
    claims_list = claims_sub.add_parser("list", help="列出判断")
    claims_list.add_argument("--task", default="")
    claims_list.add_argument("--pending", action="store_true", help="只看待确认")
    claims_list.add_argument("-v", "--verbose", action="store_true")
    claims_list.set_defaults(func=cmd_claims_list)

    claims_show = claims_sub.add_parser("show", help="查看判断与版本历史")
    claims_show.add_argument("id")
    claims_show.add_argument("--task", default="")
    claims_show.add_argument("--json", action="store_true")
    claims_show.set_defaults(func=cmd_claims_show)

    claims_confirm = claims_sub.add_parser("confirm", help="确认待确认修改")
    claims_confirm.add_argument("id")
    claims_confirm.add_argument("--task", default="")
    claims_confirm.add_argument("--note", default="")
    claims_confirm.set_defaults(func=cmd_claims_confirm)

    claims_reject = claims_sub.add_parser("reject", help="驳回待确认修改")
    claims_reject.add_argument("id")
    claims_reject.add_argument("--task", default="")
    claims_reject.add_argument("--note", default="")
    claims_reject.set_defaults(func=cmd_claims_reject)

    claims_rollback = claims_sub.add_parser("rollback", help="回滚到历史版本")
    claims_rollback.add_argument("id")
    claims_rollback.add_argument("--to", type=int, required=True, help="目标版本号")
    claims_rollback.add_argument("--task", default="")
    claims_rollback.add_argument("--note", default="")
    claims_rollback.set_defaults(func=cmd_claims_rollback)

    claims_retire = claims_sub.add_parser("retire", help="停用判断")
    claims_retire.add_argument("id")
    claims_retire.add_argument("--task", default="")
    claims_retire.add_argument("--note", default="")
    claims_retire.set_defaults(func=cmd_claims_retire)

    feedback = sub.add_parser("feedback", help="记录用户反馈（有用/无关/事实有误/证据不足/分类有误）")
    feedback.add_argument("verdict", choices=VERDICT_KEYS)
    feedback.add_argument("--claim", default="")
    feedback.add_argument("--signal", default="")
    feedback.add_argument("--run", default="")
    feedback.add_argument("--task", default="")
    feedback.add_argument("--note", default="")
    feedback.set_defaults(func=cmd_feedback)

    metrics = sub.add_parser("metrics", help="查看验证指标")
    metrics.add_argument("--task", default="")
    metrics.add_argument("--json", action="store_true")
    metrics.set_defaults(func=cmd_metrics)

    serve_cmd = sub.add_parser("serve", help="启动本地 Web 界面")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8765)
    serve_cmd.add_argument("--open", action="store_true")
    serve_cmd.set_defaults(func=cmd_serve)

    validate = sub.add_parser("validate", help="校验领域配置")
    validate.set_defaults(func=cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = Context(args)
    try:
        return int(args.func(args, ctx) or 0)
    except ProfileError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断。", file=sys.stderr)
        return 130


def _configure_console() -> None:
    """避免 Windows GBK 控制台因特殊符号或非本地字符直接崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):  # pragma: no cover - 非标准流
            continue


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
