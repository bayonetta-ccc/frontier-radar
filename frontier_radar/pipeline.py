"""一次完整运行：采集 → 去重 → 分类 → 映射 → 判断更新 → 报告。

运行结果（Digest）同时落库与导出，任何一次结论都可以回到：
哪条信号、哪个来源、哪次判断版本、为什么。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .claims import ClaimEngine, Evaluation, Thresholds
from .db import Database
from .evidence import Classifier
from .models import (
    CHANGE_LABELS,
    Change,
    ChangeKind,
    Claim,
    Digest,
    SignalJudgement,
    SignalRecord,
)
from .profile import Profile, load_profile
from .sources import IngestResult, SourceAccess, collector_for
from .text import (
    clean_text,
    clamp,
    confidence_label,
    content_hash,
    iso_date,
    iso_now,
    jaccard,
    parse_iso,
    tokens,
    truncate,
    url_key,
)


@dataclass
class PipelineResult:
    digest: Digest
    evaluations: list[Evaluation]
    ingest: IngestResult
    signals: list[SignalRecord]
    baseline_claims: list[Claim] = field(default_factory=list)
    run_id: str = ""

    @property
    def changed(self) -> list[Change]:
        return self.digest.changes


class RadarPipeline:
    def __init__(
        self,
        db: Database,
        profile: Profile | str,
        *,
        fixtures_dir: str | Path = "config/fixtures",
        thresholds: Thresholds | None = None,
        artifact_root: str | Path | None = None,
    ) -> None:
        self.db = db
        self.profile = load_profile(profile) if isinstance(profile, str) else profile
        self.classifier = Classifier(self.profile)
        self.engine = ClaimEngine(db, self.profile, self.classifier, thresholds)
        self.fixtures_dir = Path(fixtures_dir)
        self.artifact_root = Path(artifact_root) if artifact_root else Path("data") / "artifacts"

    # ------------------------------------------------------------------ 任务
    def create_task(self, payload: dict[str, Any]) -> str:
        now = iso_now()
        task_id = str(payload.get("task_id") or f"task_{now[:10].replace('-', '')}_{self.profile.slug}")
        task = {
            "task_id": task_id,
            "topic": payload.get("topic") or self.profile.name,
            "role": payload.get("role", ""),
            "purpose": payload.get("purpose", ""),
            "decision": payload.get("decision", ""),
            "constraints": payload.get("constraints", ""),
            "horizon": payload.get("horizon", ""),
            "decision_criteria": payload.get("decision_criteria", ""),
            "watchlist": payload.get("watchlist", []),
            "notes": payload.get("notes", ""),
            "profile_slug": self.profile.slug,
            "high_risk_threshold": payload.get("high_risk_threshold", 0.6),
            "created_at": now,
            "updated_at": now,
        }
        self.db.create_task(task)
        self.db.upsert_sources(
            task_id,
            [
                {
                    "source_id": s.id,
                    "name": s.name,
                    "kind": s.kind,
                    "authority": s.authority,
                    "cadence": s.cadence,
                    "url": s.url,
                    "note": s.note,
                    "enabled": 1,
                }
                for s in self.profile.sources
            ],
        )
        self.db.add_event(task_id, "task_created", {"topic": task["topic"]})
        return task_id

    # ------------------------------------------------------------------ 运行
    def run(
        self,
        task_id: str,
        *,
        mode: str = "auto",  # auto / baseline / update
        source_mode: str = "offline",
        now: str | None = None,
        period_days: int = 7,
        auto_confirm: bool = False,
        confirm_after: bool = False,
    ) -> PipelineResult:
        task = self.db.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")

        run_time = parse_iso(now or iso_now())
        previous = self.db.last_finished_run(task_id)
        actual_mode = mode
        if mode == "auto":
            if previous is None or not self.db.list_claims(task_id):
                actual_mode = "baseline"
            else:
                actual_mode = "update"
        period_end = run_time
        period_start = (
            parse_iso(previous["finished_at"])
            if previous
            else run_time - timedelta(days=period_days)
        )
        if previous and parse_iso(previous["finished_at"]) >= run_time:
            raise ValueError("运行时间必须晚于上一次运行，避免时间线倒流。")

        run_id = f"run_{run_time.strftime('%Y%m%dT%H%M%S')}_{actual_mode}"
        suffix = 1
        while self.db.get_run(run_id):
            suffix += 1
            run_id = f"run_{run_time.strftime('%Y%m%dT%H%M%S')}_{actual_mode}_{suffix}"
        started = iso_now()
        self.db.create_run(
            {
                "run_id": run_id,
                "task_id": task_id,
                "mode": actual_mode,
                "period_start": period_start.isoformat(timespec="seconds"),
                "period_end": period_end.isoformat(timespec="seconds"),
                "started_at": started,
                # 以业务时间（run 的 now）作为完成时间，保证可重放与时间线单调
                "finished_at": run_time.isoformat(timespec="seconds"),
                "source_mode": source_mode,
                "status": "running",
                "summary": {},
            }
        )

        sources = [s for s in self.profile.sources if self._source_enabled(task_id, s.id)]
        ingest = collector_for(self.profile, source_mode, self.fixtures_dir).fetch(sources)
        in_window = [
            s
            for s in ingest.signals
            if period_start.replace(microsecond=0) <= parse_iso(s.published_at) <= period_end
        ]
        self._mark_access(task_id, ingest.access, in_window)

        records, judgements = self._normalize_and_classify(
            task, run_id, in_window, period_start, period_end, ingest
        )
        # 先把本期信号落库，判断引擎才能在同一事务视图里引用它们
        self.db.insert_signals(task_id, run_id, records)
        judgement_map = {j.signal_id: j for j in judgements}
        signal_map = {r.signal_id: _record_dict(r) for r in records}
        # 只让“有效且未去重”的信号参与判断
        active_judgements = [
            j for j in judgements if not j.noise_reason and not signal_map[j.signal_id]["duplicate_of"]
        ]

        baseline_claims: list[Claim] = []
        evaluations: list[Evaluation] = []
        if actual_mode == "baseline":
            baseline_claims = self.engine.bootstrap(
                task, run_id, active_judgements, signal_map, now=run_time.isoformat()
            )
            evaluations = []
        else:
            if auto_confirm:
                self._auto_confirm_pending(task_id, now=run_time.isoformat())
            evaluations = self.engine.evaluate(
                task, run_id, active_judgements, signal_map, now=run_time.isoformat()
            )
            if confirm_after:
                self._auto_confirm_pending(
                    task_id,
                    note="演示模式：本期结束后统一确认待确认修改",
                    now=run_time.isoformat(),
                )

        self._attach_signal_usage(ingest.access, records)

        digest = self._build_digest(
            task, run_id, actual_mode, run_time, period_start, period_end,
            ingest, records, judgement_map, evaluations, baseline_claims, previous,
        )
        paths = export_digest(
            digest,
            self.artifact_root / task_id / run_id,
            [_record_dict(record) for record in records],
        )
        for kind, path in paths.items():
            self.db.add_artifact(run_id, task_id, kind, str(path))

        status = "ok" if not ingest.errors or ingest.signals else "partial"
        self.db.finish_run(run_id, status, _summary_payload(digest))
        self.db.add_event(
            task_id,
            "run_finished",
            {"run_id": run_id, "mode": actual_mode, "changes": len(digest.changes)},
            run_id,
        )
        return PipelineResult(
            digest=digest,
            evaluations=evaluations,
            ingest=ingest,
            signals=records,
            baseline_claims=baseline_claims,
            run_id=run_id,
        )

    # ------------------------------------------------------------------ 组装
    def _source_enabled(self, task_id: str, source_id: str) -> bool:
        for source in self.db.list_sources(task_id):
            if source["source_id"] == source_id:
                return bool(source["enabled"])
        return True

    def _mark_access(self, task_id: str, access: list[SourceAccess], fresh: list) -> None:
        counts: dict[str, int] = {}
        for signal in fresh:
            counts[signal.source_id] = counts.get(signal.source_id, 0) + 1
        for item in access:
            item.used = counts.get(item.source_id, 0)
            self.db.mark_source_access(
                task_id, item.source_id, item.status == "ok", item.note
            )

    def _attach_signal_usage(self, access: list[SourceAccess], records: list[SignalRecord]) -> None:
        counts: dict[str, int] = {}
        for record in records:
            if not record.is_noise and not record.is_duplicate:
                counts[record.source_id] = counts.get(record.source_id, 0) + 1
        for item in access:
            item.used = counts.get(item.source_id, 0)

    def _normalize_and_classify(
        self,
        task: dict,
        run_id: str,
        signals: Iterable,
        period_start: datetime,
        period_end: datetime,
        ingest: IngestResult,
    ) -> tuple[list[SignalRecord], list[SignalJudgement]]:
        del period_start, period_end
        prior = self.db.find_recent_signals(task["task_id"], before=iso_now(), limit=400)
        prior_titles = [s["title"] for s in prior]
        seen_url_keys = {s["url_key"] for s in prior}
        seen_hashes = {s["content_hash"] for s in prior}
        prior_index = [
            (s["signal_id"], s["title"], s["url_key"], s["content_hash"], s["published_at"])
            for s in prior
        ]
        batch: list[tuple[Any, str, str]] = []  # (signal, url_key, content_hash)

        records: list[SignalRecord] = []
        judgements: list[SignalJudgement] = []
        for signal in signals:
            key = url_key(signal.url)
            digest = content_hash(signal.title, signal.excerpt)
            duplicate_of: str | None = None
            reason = ""
            if key and key in seen_url_keys:
                existing = self.db.find_url_key(task["task_id"], key)
                if existing and existing.get("content_hash") == digest:
                    duplicate_of = str(existing.get("signal_id") or "")
                    reason = "同一 URL 已在上一次运行中出现（内容未变化）"
                # URL 相同但内容变化：作为新信号处理，不当作重复
            if not duplicate_of and digest in seen_hashes:
                duplicate_of = next(
                    (s["signal_id"] for s in prior if s["content_hash"] == digest), ""
                )
                reason = reason or "正文与历史信号完全一致（重复转载）"
            if not duplicate_of:
                for prior_signal_id, prior_title, _pkey, _phash, _pdate in prior_index:
                    if jaccard(tokens(signal.title), tokens(prior_title)) >= 0.8:
                        duplicate_of = prior_signal_id
                        reason = "标题与历史信号高度相似，判定为同一事件转载"
                        break
            if not duplicate_of:
                for other_signal, other_key, other_digest in batch:
                    if key and key == other_key:
                        duplicate_of = other_signal.signal_id
                        reason = "同一 URL 在本批次重复出现"
                        break
                    if digest == other_digest:
                        duplicate_of = other_signal.signal_id
                        reason = "正文与本批次另一条信号完全一致（同稿多投）"
                        break
                    if jaccard(tokens(signal.title), tokens(other_signal.title)) >= 0.82:
                        duplicate_of = other_signal.signal_id
                        reason = "标题高度相似，判定为同一事件转载"
                        break
            batch.append((signal, key, digest))

            judgement = self.classifier.classify(signal, prior_titles)
            judgement.relevance = self.classifier.relevance_to_task(judgement, task)
            if duplicate_of:
                # 重复信号单独记录，不覆盖噪声原因（两类在报告里要分开统计）
                duplicate_note = reason or "重复信号"
                judgement.notes.append(duplicate_note)
            record = SignalRecord(
                signal_id=signal.signal_id,
                run_id=run_id,
                source_id=signal.source_id,
                title=signal.title,
                url=signal.url,
                url_key=key,
                published_at=signal.published_at,
                retrieved_at=signal.retrieved_at,
                excerpt=signal.excerpt,
                category=judgement.category,
                info_layer=judgement.info_layer,
                stage_level=judgement.stage,
                stage_label=self.profile.stage_label(judgement.stage),
                entities=judgement.entities,
                anchors=judgement.anchors,
                novelty=judgement.novelty,
                noise_reason=judgement.noise_reason,
                duplicate_of=duplicate_of,
                canonical_group=key or digest,
                content_hash=digest,
                reliability=judgement.reliability,
                raw={"source_kind": self.profile.source_kind(signal.source_id), **signal.raw},
            )
            records.append(record)
            judgements.append(judgement)
            if not record.is_noise and not record.is_duplicate:
                seen_url_keys.add(key)
                seen_hashes.add(digest)
        return records, judgements

    # ------------------------------------------------------------------ 汇总
    def _build_digest(
        self,
        task: dict,
        run_id: str,
        mode: str,
        run_time: datetime,
        period_start: datetime,
        period_end: datetime,
        ingest: IngestResult,
        records: list[SignalRecord],
        judgement_map: dict[str, SignalJudgement],
        evaluations: list[Evaluation],
        baseline_claims: list[Claim],
        previous: dict | None,
    ) -> Digest:
        claims = self.db.list_claims(task["task_id"])
        changes = [e.change for e in evaluations if e.change and e.status in {"applied", "pending"}]
        changes.sort(key=lambda c: (c.requires_confirmation, c.score), reverse=True)

        pending = [c for c in changes if c.requires_confirmation]
        applied = [c for c in changes if not c.requires_confirmation]
        duplicates = [r for r in records if r.duplicate_of]
        noise = [r for r in records if r.is_noise and not r.duplicate_of]
        untouched = [
            e
            for e in evaluations
            if e.status == "ignored" and e.new_signal_ids
        ]
        unresolved = [e for e in evaluations if e.status == "unresolved"]

        mode_label = (
            "首次基线"
            if mode == "baseline"
            else ("本期无实质变化" if not changes else "变化更新")
        )
        summary = {
            "mode": mode,
            "mode_label": mode_label,
            "signal_total": len(records),
            "signal_unique": len(records) - len(duplicates),
            "signal_effective": len(records) - len(duplicates) - len(noise),
            "duplicate_count": len(duplicates),
            "noise_count": len(noise),
            "change_applied": len(applied),
            "change_pending": len(pending),
            "unresolved_count": len(unresolved),
            "no_progress_count": len(untouched),
            "claim_total": len(claims),
            "claims_active": sum(1 for c in claims if c.status.value in {"active", "weakened"}),
            "headline": _headline(
                mode, changes, pending, untouched, unresolved, duplicates, noise,
                baseline_count=len(baseline_claims),
                pending_claims=len([c for c in claims if c.pending_review]),
            ),
            "by_kind": _count_by_kind(changes),
            "previous_run": previous["run_id"] if previous else "",
            "previous_period_end": previous["finished_at"] if previous else "",
        }

        timeline = _build_timeline(records, judgement_map, period_start, period_end)
        not_pushed = _build_not_pushed(records, judgement_map, evaluations)
        coverage = _build_coverage(self.profile, ingest, records, claims)
        coverage["window"] = {
            "period_start": period_start.isoformat(timespec="seconds"),
            "period_end": period_end.isoformat(timespec="seconds"),
        }
        metrics = _build_metrics(records, judgement_map, changes, duplicates, noise, claims, self.profile)
        relevance = _build_relevance(task, claims, changes, pending)
        claims_snapshot = [_claim_snapshot(c) for c in claims]
        profile_info = {
            "slug": self.profile.slug,
            "name": self.profile.name,
            "description": self.profile.description,
            "stage_scale": [{"level": s.level, "label": s.label} for s in self.profile.stages],
        }
        return Digest(
            run_id=run_id,
            task_id=task["task_id"],
            run_at=run_time.isoformat(timespec="seconds"),
            period_start=period_start.isoformat(timespec="seconds"),
            period_end=period_end.isoformat(timespec="seconds"),
            mode=mode,
            summary=summary,
            changes=changes,
            timeline=timeline,
            not_pushed=not_pushed,
            coverage=coverage,
            relevance=relevance,
            metrics=metrics,
            claims=claims_snapshot,
            profile=profile_info,
        )

    def _auto_confirm_pending(
        self, task_id: str, note: str = "auto-confirm（演示模式）", now: str | None = None
    ) -> None:
        """仅用于演示/批量验证：把上一期待确认的修改按当前证据确认。"""
        for claim in self.db.list_claims(task_id):
            if claim.pending_review:
                try:
                    self.engine.confirm(claim.claim_id, note=note, now=now)
                except ValueError:
                    continue


# --------------------------------------------------------------------- 函数


def _claim_snapshot(claim: Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "statement": claim.statement,
        "status": claim.status.value,
        "confidence": claim.confidence,
        "source_reliability": claim.source_reliability,
        "impact_confidence": claim.impact_confidence,
        "stage_level": claim.stage_level,
        "high_risk": claim.high_risk,
        "pending_review": claim.pending_review,
        "needs_review": claim.needs_review,
        "claim_kind": claim.claim_kind,
        "open_questions": list(claim.open_questions),
        "watch_indicators": list(claim.watch_indicators),
        "next_verification": claim.next_verification,
        "supporting_signal_ids": list(claim.supporting_signal_ids),
        "contradicting_signal_ids": list(claim.contradicting_signal_ids),
        "updated_at": claim.updated_at,
        "version": claim.version,
    }


def _record_dict(record: SignalRecord) -> dict[str, Any]:
    return {
        "signal_id": record.signal_id,
        "source_id": record.source_id,
        "title": record.title,
        "url": record.url,
        "published_at": record.published_at,
        "excerpt": record.excerpt,
        "category": record.category,
        "info_layer": record.info_layer.value,
        "stage_level": record.stage_level,
        "stage_label": record.stage_label,
        "entities": list(record.entities),
        "anchors": list(record.anchors),
        "novelty": record.novelty,
        "noise_reason": record.noise_reason,
        "duplicate_of": record.duplicate_of,
        "reliability": record.reliability,
    }


def _headline(
    mode: str,
    changes: list[Change],
    pending: list[Change],
    untouched: list[Evaluation],
    unresolved: list[Evaluation],
    duplicates: list,
    noise: list,
    baseline_count: int = 0,
    pending_claims: int = 0,
) -> str:
    if mode == "baseline":
        return (
            f"已建立初始基线：{baseline_count} 个待确认判断，"
            f"另有 {len(duplicates)} 条重复与 {len(noise)} 条噪声未进入判断。"
        )
    if not changes and not pending:
        suffix = f"另有 {pending_claims} 个判断仍等你确认。" if pending_claims else ""
        return (
            f"本期无需更新：{len(duplicates) + len(noise)} 条信号未带来实质变化，"
            f"仍有 {len(unresolved)} 个问题缺乏足够证据。{suffix}"
        )
    bits = []
    if pending:
        bits.append(f"{len(pending)} 个判断待确认")
    if changes:
        counter: dict[str, int] = {}
        for change in changes:
            counter[change.kind.value] = counter.get(change.kind.value, 0) + 1
        detail = "、".join(
            f"{CHANGE_LABELS[ChangeKind(kind)]}{count}" for kind, count in counter.items()
        )
        bits.append(f"{len(changes)} 个判断需要更新（{detail}）")
    if untouched:
        bits.append(f"{len(untouched)} 个热门信号未带来实质进展")
    if duplicates or noise:
        bits.append(f"{len(duplicates) + len(noise)} 条重复/噪声被拦截")
    return "；".join(bits) + "。"


def _count_by_kind(changes: list[Change]) -> dict[str, int]:
    counts: dict[str, int] = {kind.value: 0 for kind in ChangeKind if kind not in {ChangeKind.NO_CHANGE, ChangeKind.UNRESOLVED}}
    for change in changes:
        counts[change.kind.value] = counts.get(change.kind.value, 0) + 1
    return counts


def _timeline_node_type(record: SignalRecord) -> str:
    if record.info_layer.value == "rumor":
        return "prediction"
    if record.category in {"mass_production_plan", "capacity"}:
        return "official_plan"
    if record.info_layer.value in {"opinion", "inference"}:
        return "prediction"
    if record.category == "standard":
        return "actual_event"
    return "actual_event"


def _build_timeline(
    records: list[SignalRecord],
    judgement_map: dict[str, SignalJudgement],
    period_start: datetime,
    period_end: datetime,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for record in records:
        if record.is_noise or record.duplicate_of:
            continue
        nodes.append(
            {
                "date": iso_date(record.published_at),
                "title": truncate(record.title, 80),
                "type": _timeline_node_type(record),
                "stage": record.stage_label,
                "source": record.source_id,
                "signal_id": record.signal_id,
                "info_layer": record.info_layer.value,
                "category": record.category,
                "url": record.url,
                "excerpt": record.excerpt,
            }
        )
    nodes.sort(key=lambda n: n["date"])
    return nodes[:24]


def _build_not_pushed(
    records: list[SignalRecord],
    judgement_map: dict[str, SignalJudgement],
    evaluations: list[Evaluation],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    by_signal: dict[str, str] = {}
    for evaluation in evaluations:
        for signal_id in evaluation.new_signal_ids:
            if evaluation.change:
                by_signal[signal_id] = evaluation.change.kind.value
    for record in records:
        if not record.is_noise and not record.duplicate_of:
            continue
        items.append(
            {
                "signal_id": record.signal_id,
                "title": truncate(record.title, 90),
                "source": record.source_id,
                "category": record.category,
                "info_layer": record.info_layer.value,
                "reason": record.noise_reason or "重复信号",
                "url": record.url,
                "published_at": record.published_at,
            }
        )
    return items[:30]


def _build_coverage(
    profile: Profile, ingest: IngestResult, records: list[SignalRecord], claims: list[Claim]
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.source_id] = counts.get(record.source_id, 0) + 1
    sources = []
    for access in ingest.access:
        sources.append(
            {
                "source_id": access.source_id,
                "name": access.name,
                "kind": access.kind,
                "authority": access.authority,
                "status": access.status,
                "fetched": access.fetched,
                "used": access.used,
                "signal_count": counts.get(access.source_id, 0),
                "note": access.note,
                "url": access.url,
            }
        )
    gaps = list(profile.coverage_notes)
    failed = [s for s in sources if s["status"] in {"unreachable", "not_configured"}]
    for item in failed:
        gaps.append(f"来源未取到：{item['name']}（{item['status']}）")
    if ingest.synthetic:
        gaps.insert(0, "本期使用合成演示数据（fixture），不代表真实世界的最新进展。")
    single_source_claims = [
        c.statement for c in claims if len(set(c.supporting_signal_ids)) <= 1 and c.status.value != "falsified"
    ]
    return {
        "mode": ingest.mode,
        "synthetic": ingest.synthetic,
        "sources_total": len(sources),
        "sources_ok": ingest.ok_sources,
        "coverage_ratio": ingest.coverage_ratio,
        "window": {
            "period_start": "",
            "period_end": "",
        },
        "sources": sources,
        "gaps": gaps,
        "single_source_claims": single_source_claims[:5],
        "open_questions": list(profile.gap_questions),
        "errors": ingest.errors[:5],
    }


def _build_metrics(
    records: list[SignalRecord],
    judgement_map: dict[str, SignalJudgement],
    changes: list[Change],
    duplicates: list[SignalRecord],
    noise: list[SignalRecord],
    claims: list[Claim],
    profile: Profile,
) -> dict[str, Any]:
    total = len(records) or 1
    effective = len(records) - len(duplicates) - len(noise)
    changed_signal_ids = {s.signal_id for change in changes for s in [*change.supporting, *change.contradicting]}
    changed = [r for r in records if r.signal_id in changed_signal_ids]
    groups = {
        (profile.source_by_id(r.source_id).independent_group if profile.source_by_id(r.source_id) else r.source_id)
        for r in changed
    }
    independent_ratio = len(changed) / max(len(changed), 1)
    link_ok = sum(1 for r in changed if r.url.startswith("http"))
    quote_ok = sum(1 for r in changed if len(clean_text(r.excerpt)) >= 24)
    with_polarity = sum(1 for c in changes if abs(c.confidence_after - c.confidence_before) > 0.001)
    layered = sum(1 for c in changes for s in [*c.supporting, *c.contradicting] if s.info_layer.value)
    claims_with_both = sum(
        1 for c in claims if c.supporting_signal_ids and c.contradicting_signal_ids
    )
    return {
        "signals_total": len(records),
        "signals_effective": effective,
        "duplicate_rate": round(len(duplicates) / total, 3),
        "noise_rate": round(len(noise) / total, 3),
        "changed_claim_source_groups": len(groups),
        "changed_claim_link_ok": round(link_ok / max(len(changed), 1), 3),
        "changed_claim_quote_ok": round(quote_ok / max(len(changed), 1), 3),
        "changes_with_confidence_delta": round(with_polarity / max(len(changes), 1), 3),
        "layered_evidence_ratio": round(layered / max(sum(len(c.supporting) + len(c.contradicting) for c in changes), 1), 3),
        "claims_with_both_sides": claims_with_both,
        "claims_total": len(claims),
        "evidence_gap_claims": sum(1 for c in claims if not c.contradicting_signal_ids),
        "note": "指标仅用于验证信息质量与更新质量，不替代用户判断。",
    }


def _build_relevance(
    task: dict, claims: list[Claim], changes: list[Change], pending: list[Change]
) -> dict[str, Any]:
    tracked = [c for c in claims if c.status.value in {"active", "weakened", "proposed"}]
    watch = [c.statement for c in tracked if c.status.value in {"active", "weakened"}][:5]
    watch_pending = [c.statement for c in tracked if c.status.value == "proposed"][:3]
    ignore = [c.statement for c in claims if c.status.value == "falsified"][:3]
    actions = []
    for change in changes[:4]:
        actions.append(
            {
                "claim_id": change.claim_id,
                "action": change.next_steps[0] if change.next_steps else "等待下一次独立来源确认。",
                "why": truncate(change.why_matters, 120),
            }
        )
    review = []
    for change in pending:
        review.append(
            {
                "claim_id": change.claim_id,
                "statement": change.claim_statement,
                "reason": "高风险判断或独立来源不足，需要人工核对后才会写入当前判断。",
            }
        )
    return {
        "role": task.get("role", ""),
        "purpose": task.get("purpose", ""),
        "decision": task.get("decision", ""),
        "constraints": task.get("constraints", ""),
        "horizon": task.get("horizon", ""),
        "watch": watch,
        "watch_pending": watch_pending,
        "ignore": ignore,
        "actions": actions,
        "needs_review": review,
        "reassess_when": _reassess_conditions(claims),
    }


def _reassess_conditions(claims: list[Claim]) -> list[str]:
    conditions: list[str] = []
    for claim in claims:
        if claim.status.value in {"active", "weakened", "proposed"}:
            for indicator in claim.watch_indicators[:2]:
                if indicator not in conditions:
                    conditions.append(indicator)
    return conditions[:6]


def _summary_payload(digest: Digest) -> dict[str, Any]:
    return {
        **digest.summary,
        "changes": [c.change_id for c in digest.changes],
        "changes_pending": [c.change_id for c in digest.changes if c.requires_confirmation],
    }


# --------------------------------------------------------------------- 导出


def export_digest(digest: Digest, directory: Path, signals: list[dict[str, Any]] | None = None) -> dict[str, str]:
    from .render import register_signals, render_markdown

    if signals is not None:
        register_signals(digest.run_id, signals)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "digest.json"
    markdown_path = directory / "digest.md"
    payload = digest.to_dict()
    if signals is not None:
        payload["signal_index"] = signals
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(digest), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def load_digest(task_id: str, run_id: str, root: str | Path = "data/artifacts") -> dict[str, Any] | None:
    path = Path(root) / task_id / run_id / "digest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["RadarPipeline", "PipelineResult", "export_digest", "load_digest"]
