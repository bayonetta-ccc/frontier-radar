"""跨运行的状态、验证指标与静态站点发布。

* ``claim_state``：把一条判断连同版本历史、证据台账序列化；
* ``build_metrics``：把多期运行汇总成可复核的质量指标与质量门；
* ``publish_site``：把报告导出为可直接发布的静态站点（无需服务器）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .db import Database
from .models import Claim
from .render import (
    render_about_page,
    render_claims_page,
    render_dashboard,
    render_run_page,
)


def claim_state(db: Database, claim: Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "statement": claim.statement,
        "status": claim.status.value,
        "confidence": claim.confidence,
        "confidence_label": _confidence_label(claim.confidence),
        "source_reliability": claim.source_reliability,
        "impact_confidence": claim.impact_confidence,
        "stage_level": claim.stage_level,
        "high_risk": claim.high_risk,
        "origin": claim.origin,
        "needs_review": claim.needs_review,
        "pending_review": claim.pending_review,
        "claim_kind": claim.claim_kind,
        "open_questions": list(claim.open_questions),
        "watch_indicators": list(claim.watch_indicators),
        "next_verification": claim.next_verification,
        "supporting_signal_ids": list(claim.supporting_signal_ids),
        "contradicting_signal_ids": list(claim.contradicting_signal_ids),
        "created_at": claim.created_at,
        "updated_at": claim.updated_at,
        "version": claim.version,
        "versions": [
            {
                "version": version.version,
                "statement": version.statement,
                "status": version.status,
                "confidence": version.confidence,
                "stage_level": version.stage_level,
                "changed_at": version.changed_at,
                "change_kind": version.change_kind,
                "reason": version.reason,
                "trigger_signal_ids": list(version.trigger_signal_ids),
                "user_correction": version.user_correction,
            }
            for version in db.list_claim_versions(claim.claim_id)
        ],
    }


def list_claim_states(db: Database, task_id: str) -> list[dict[str, Any]]:
    return [claim_state(db, claim) for claim in db.list_claims(task_id)]


def _confidence_label(value: float) -> str:
    if value >= 0.75:
        return "较高"
    if value >= 0.55:
        return "中等"
    if value >= 0.35:
        return "偏低"
    return "低"


def build_metrics(db: Database, task_id: str) -> dict[str, Any]:
    runs = list(reversed(db.list_runs(task_id, limit=100)))
    task = db.get_task(task_id) or {}
    claims = db.list_claims(task_id, statuses=None)
    feedback = db.list_feedback(task_id, limit=500)
    events = db.list_events(task_id, limit=500)

    changes: list[dict[str, Any]] = []
    noise_rates: list[float] = []
    duplicate_rates: list[float] = []
    source_groups: list[int] = []
    coverage: list[float] = []
    total_signals = 0
    total_duplicates = 0
    total_noise = 0
    run_rows: list[dict[str, Any]] = []
    for run in runs:
        summary = run.get("summary", {})
        digest = _load_digest(db, task_id, run["run_id"])
        if digest:
            metrics = digest.get("metrics", {})
            noise_rates.append(float(metrics.get("noise_rate", 0)))
            duplicate_rates.append(float(metrics.get("duplicate_rate", 0)))
            source_groups.append(int(metrics.get("changed_claim_source_groups", 0)))
            coverage.append(float(digest.get("coverage", {}).get("coverage_ratio", 0)))
            changes.extend(digest.get("changes", []))
            total_signals += int(metrics.get("signals_total", 0))
            total_duplicates += int(summary.get("duplicate_count", 0))
            total_noise += int(summary.get("noise_count", 0))
        run_rows.append(
            {
                "run_id": run["run_id"],
                "mode": run["mode"],
                "period_end": run["period_end"][:10],
                "signals": summary.get("signal_total", 0),
                "changes": summary.get("change_applied", 0) + summary.get("change_pending", 0),
                "pending": summary.get("change_pending", 0),
                "duplicate": summary.get("duplicate_count", 0),
                "noise": summary.get("noise_count", 0),
            }
        )

    feedback_counts: dict[str, int] = {}
    for item in feedback:
        feedback_counts[item["verdict"]] = feedback_counts.get(item["verdict"], 0) + 1

    linked = [
        c
        for c in changes
        if c.get("supporting") or c.get("contradicting")
    ]
    linked = [c for c in linked if _all_have_links(c)]
    denied = [c for c in changes if c.get("kind") == "falsify"]
    strengthened = [c for c in changes if c.get("kind") == "strengthen"]
    single_source = [c for c in changes if c.get("kind") in {"strengthen", "weaken", "falsify"} and len(c.get("supporting", [])) + len(c.get("contradicting", [])) <= 1]
    pending_now = [c for c in claims if c.pending_review]
    versions = [v for c in claims for v in db.list_claim_versions(c.claim_id) if v.change_kind not in {"baseline"}]
    with_reason = [v for v in versions if v.reason.strip()]

    quality = {
        "duplicate_rate": round(total_duplicates / max(total_signals, 1), 3),
        "noise_rate": round(total_noise / max(total_signals, 1), 3),
        "duplicate_rate_avg": _avg(duplicate_rates),
        "noise_rate_avg": _avg(noise_rates),
        "link_integrity": round(len(linked) / max(len(changes), 1), 3),
        "changes_total": len(changes),
        "changes_with_single_source": len(single_source),
        "change_source_groups": source_groups,
        "coverage_ratio_avg": _avg(coverage),
        "claims_total": len(claims),
        "claims_with_contradicting_evidence": sum(1 for c in claims if c.contradicting_signal_ids),
        "claims_with_open_questions": sum(1 for c in claims if c.open_questions),
    }
    updates = {
        "versions_total": len(versions),
        "versions_with_reason": round(len(with_reason) / max(len(versions), 1), 3),
        "falsified": sum(1 for c in claims if c.status.value == "falsified"),
        "weakened": sum(1 for c in claims if c.status.value == "weakened"),
        "pending_review": len(pending_now),
        "rolled_back": sum(1 for v in versions if v.change_kind == "rollback"),
        "user_corrections": len([v for v in versions if v.user_correction in {"confirmed", "rollback", "rejected"}]),
        "feedback_total": sum(feedback_counts.values()),
        "feedback": feedback_counts,
    }
    value = {
        "runs": len(runs),
        "periods_with_changes": sum(1 for row in run_rows if row["changes"]),
        "quiet_periods": sum(1 for row in run_rows if not row["changes"] and row["mode"] != "baseline"),
        "changes_per_period": round(len(changes) / max(len([r for r in runs if r["mode"] != "baseline"]), 1), 2),
        "claims_tracked": len(claims),
        "retention_signal": _retention_signal(runs, claims, task),
    }
    gates = _quality_gates(quality, updates)
    return {
        "task": {
            "task_id": task_id,
            "topic": task.get("topic", ""),
            "role": task.get("role", ""),
            "decision": task.get("decision", ""),
            "runs": len(runs),
        },
        "information_quality": quality,
        "judgement_update_quality": updates,
        "user_value_proxies": value,
        "quality_gates": gates,
        "runs_detail": run_rows,
        "events": len(events),
        "note": "这些是过程指标，不是成功证明；真正的验证问题是：用户是否愿意让系统长期保存并更新自己的判断。",
    }


def _quality_gates(quality: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    checks = [
        {
            "key": "duplicate_rate",
            "label": "重复率 ≤ 15%",
            "value": quality["duplicate_rate"],
            "pass": quality["duplicate_rate"] <= 0.15,
            "note": "同稿多投、转载与追踪参数造成的重复应被去重拦截。",
        },
        {
            "key": "noise_rate",
            "label": "噪声率 ≤ 35%",
            "value": quality["noise_rate"],
            "pass": quality["noise_rate"] <= 0.35,
            "note": "营销、观点与传闻占全部信号的比例。",
        },
        {
            "key": "link_integrity",
            "label": "变化卡链接完整率 = 100%",
            "value": quality["link_integrity"],
            "pass": quality["link_integrity"] >= 0.999,
            "note": "每条进入判断的变化都必须能回到原始链接。",
        },
        {
            "key": "update_reason",
            "label": "判断变更 100% 留下理由",
            "value": updates["versions_with_reason"],
            "pass": updates["versions_with_reason"] >= 0.999,
            "note": "没有理由的修改等于不可审计的修改。",
        },
        {
            "key": "human_review",
            "label": "高风险修改必须人工确认",
            "value": updates["pending_review"],
            "pass": True,
            "note": "待确认数量不为 0 不是失败：它说明系统没有绕过人工复核。",
        },
    ]
    return {
        "pass": all(item["pass"] for item in checks),
        "checks": checks,
    }


def _retention_signal(runs: list[dict], claims: list[Claim], task: dict) -> dict[str, Any]:
    if not runs:
        return {"level": "无数据", "reason": "尚无运行记录。"}
    periods = len([r for r in runs if r["mode"] != "baseline"])
    if periods >= 3 and claims:
        return {
            "level": "具备持续追踪迹象",
            "reason": f"已连续运行 {periods} 个更新周期，并维护 {len(claims)} 条判断；"
            "但真正的留存仍需观察用户是否主动回来看“相较上次变了什么”。",
        }
    return {
        "level": "尚在验证",
        "reason": f"目前只有 {periods} 个更新周期，还不足以判断用户是否愿意长期托管判断。",
    }


def _all_have_links(change: dict[str, Any]) -> bool:
    relevant = [*change.get("supporting", []), *change.get("contradicting", [])]
    if not relevant:
        return False
    return all(bool(item.get("url")) for item in relevant)


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def _load_digest(db: Database, task_id: str, run_id: str) -> dict[str, Any] | None:
    for artifact in db.artifacts_for_run(run_id):
        if artifact["kind"] == "json":
            path = Path(artifact["path"])
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
    return None


# --------------------------------------------------------------------- 发布


def _register_digest_signals(digest: dict[str, Any]) -> None:
    from .render import register_signals

    records = digest.get("signal_index")
    if records:
        register_signals(str(digest.get("run_id", "")), records)


def publish_site(data_dir: str | Path, task_id: str) -> Path | None:
    """把已有运行导出为静态站点（index.html / claims.html / <run_id>.html）。"""
    data_dir = Path(data_dir)
    db_path = data_dir / "radar.db"
    if not db_path.exists():
        return None
    db = Database(db_path)
    try:
        task = db.get_task(task_id)
        if not task:
            return None
        runs = db.list_runs(task_id, limit=100)
        if not runs:
            return None
        claims = [claim_state(db, claim) for claim in db.list_claims(task_id)]
        site = data_dir / "web"
        site.mkdir(parents=True, exist_ok=True)
        digests: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for run in runs:
            digest = _load_digest(db, task_id, run["run_id"])
            if digest:
                digests.append((run, digest))
        for run, digest in digests:
            _register_digest_signals(digest)
            html_text = render_run_page(_digest_to_obj(digest))
            (site / f"{run['run_id']}.html").write_text(html_text, encoding="utf-8")
        index = render_dashboard(
            [dict(run, summary=_summary_from_digest(d)) for run, d in digests],
            task,
            claims[:8],
        )
        (site / "index.html").write_text(index, encoding="utf-8")
        (site / "claims.html").write_text(
            render_claims_page(claims, task), encoding="utf-8"
        )
        (site / "about.html").write_text(
            render_about_page(task, str(task.get("profile_slug", ""))), encoding="utf-8"
        )
        css = data_dir / "web" / "style.css"
        if not css.exists():
            from .render import CSS

            css.write_text(CSS, encoding="utf-8")
        return site
    finally:
        db.close()


def _digest_to_obj(digest: dict[str, Any]):
    from .models import Change, ChangeKind, Digest, InfoLayer, SignalJudgement

    def judgement(item: dict[str, Any]) -> SignalJudgement:
        return SignalJudgement(
            signal_id=item.get("signal_id", ""),
            category=item.get("category", ""),
            info_layer=InfoLayer(item.get("info_layer", "fact")),
            stage=int(item.get("stage", 0)),
            entities=tuple(item.get("entities", [])),
            anchors=tuple(item.get("anchors", [])),
            novelty=float(item.get("novelty", 0)),
            evidence_strength=float(item.get("evidence_strength", 0)),
            reliability=str(item.get("reliability", "中")),
            relevance=float(item.get("relevance", 0)),
            noise_reason=item.get("noise_reason", ""),
            notes=list(item.get("notes", [])),
        )

    changes = [
        Change(
            change_id=item["change_id"],
            claim_id=item["claim_id"],
            claim_statement=item["claim_statement"],
            kind=ChangeKind(item["kind"]),
            confidence_before=float(item["confidence_before"]),
            confidence_after=float(item["confidence_after"]),
            stage_before=int(item["stage_before"]),
            stage_after=int(item["stage_after"]),
            score=float(item["score"]),
            headline=item.get("headline", ""),
            why_changed=item.get("why_changed", ""),
            why_matters=item.get("why_matters", ""),
            relevance_to_user=item.get("relevance_to_user", ""),
            next_steps=tuple(item.get("next_steps", [])),
            open_questions=tuple(item.get("open_questions", [])),
            supporting=tuple(judgement(x) for x in item.get("supporting", [])),
            contradicting=tuple(judgement(x) for x in item.get("contradicting", [])),
            high_risk=bool(item.get("high_risk")),
            requires_confirmation=bool(item.get("requires_confirmation")),
            version_after=int(item.get("version_after", 0)),
        )
        for item in digest.get("changes", [])
    ]
    return Digest(
        run_id=digest["run_id"],
        task_id=digest["task_id"],
        run_at=digest["run_at"],
        period_start=digest["period_start"],
        period_end=digest["period_end"],
        mode=digest.get("mode", "update"),
        summary=digest.get("summary", {}),
        changes=changes,
        timeline=digest.get("timeline", []),
        not_pushed=digest.get("not_pushed", []),
        coverage=digest.get("coverage", {}),
        relevance=digest.get("relevance", {}),
        metrics=digest.get("metrics", {}),
        claims=digest.get("claims", []),
        profile=digest.get("profile", {}),
    )


def _summary_from_digest(digest: dict[str, Any]) -> dict[str, Any]:
    return digest.get("summary", {})


__all__ = [
    "claim_state",
    "list_claim_states",
    "build_metrics",
    "publish_site",
]
