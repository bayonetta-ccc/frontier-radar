"""判断状态库：从“信号”到“有证据、有边界、可回滚的判断更新”。

这是整个产品的核心。它只做四件事：

1. 把新证据映射到已有判断（支持 / 反向 / 中性）；
2. 用证据门槛决定“增强 / 削弱 / 证伪 / 转向 / 不更新”；
3. 每次修改都留下版本、理由、触发证据与待验证问题；
4. 高风险或证据不足时，停在“待确认”，不自动改判断。

与新闻摘要的区别：这里保存的是“当前相信什么”，以及“为什么现在还相信”。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .db import Database
from .evidence import Classifier, ClaimImpact, EvidenceAssessment, _accumulate
from .models import (
    Change,
    ChangeKind,
    Claim,
    ClaimStatus,
    ClaimVersion,
    InfoLayer,
    SignalJudgement,
)
from .profile import Anchor, Profile
from .text import clamp, confidence_label, iso_now, truncate


@dataclass
class Evaluation:
    anchor_key: str
    claim: Claim | None
    kind: ChangeKind
    status: str  # applied / pending / unresolved / ignored
    reason: str
    support_weight: float
    contra_weight: float
    projected_confidence: float
    independent_groups: int
    support: list[EvidenceAssessment] = field(default_factory=list)
    contra: list[EvidenceAssessment] = field(default_factory=list)
    neutral: list[EvidenceAssessment] = field(default_factory=list)
    change: Change | None = None
    version: int = 0
    new_signal_ids: list[str] = field(default_factory=list)
    judgements: dict[str, SignalJudgement] = field(default_factory=dict)
    proposal: dict[str, object] = field(default_factory=dict)


@dataclass
class Thresholds:
    """证据门槛。默认值偏保守：宁可不更新，也不要被单一来源带偏。"""

    strengthen_min: float = 0.12
    weaken_min: float = 0.10
    falsify_contra: float = 0.55
    falsify_support_cap: float = 0.6
    min_independent_groups: int = 2
    low_confidence_cap: float = 0.3
    high_risk_review: bool = True
    # 单次运行的置信度移动上限：证据再好，一期也只挪一小步
    max_up: float = 0.12
    max_down: float = 0.18


class ClaimEngine:
    def __init__(
        self,
        db: Database,
        profile: Profile,
        classifier: Classifier | None = None,
        thresholds: Thresholds | None = None,
    ) -> None:
        self.db = db
        self.profile = profile
        self.classifier = classifier or Classifier(profile)
        self.thresholds = thresholds or Thresholds()

    # ------------------------------------------------------------------ 基线
    def bootstrap(
        self,
        task: dict,
        run_id: str,
        judgements: Sequence[SignalJudgement],
        signals: dict[str, dict],
        now: str | None = None,
    ) -> list[Claim]:
        """首次追踪：建立 3—5 个关键判断，全部标记为“待用户确认”。"""
        now = now or iso_now()
        grouped: dict[str, list[SignalJudgement]] = {}
        for judgement in judgements:
            if judgement.noise_reason:
                continue
            for anchor_key in judgement.anchors:
                grouped.setdefault(anchor_key, []).append(judgement)

        scored: list[tuple[float, str, list[SignalJudgement]]] = []
        skip_anchors = {a.key for a in self.profile.anchors if not a.create_baseline_claim}
        for anchor_key, items in grouped.items():
            if anchor_key in skip_anchors:
                continue
            unique: dict[str, SignalJudgement] = {}
            for item in items:
                unique.setdefault(item.signal_id, item)
            values = list(unique.values())
            if not values:
                continue
            mean = sum(v.evidence_strength for v in values) / len(values)
            score = len(values) * 0.35 + mean
            scored.append((score, anchor_key, values))
        scored.sort(key=lambda x: x[0], reverse=True)

        created: list[Claim] = []
        for _score, anchor_key, values in scored[:5]:
            anchor = next((a for a in self.profile.anchors if a.key == anchor_key), None)
            if anchor is None:
                continue
            if self.db.find_claim_by_anchor(task["task_id"], anchor_key):
                continue
            claim = self._build_baseline_claim(task, anchor, values, signals, run_id, now)
            self.db.upsert_claim(claim)
            self.db.add_claim_version(
                ClaimVersion(
                    claim_id=claim.claim_id,
                    version=1,
                    statement=claim.statement,
                    status=claim.status.value,
                    confidence=claim.confidence,
                    stage_level=claim.stage_level,
                    changed_at=now,
                    change_kind="baseline",
                    reason="首次追踪：由本期证据建立的初始基线，等待用户确认后再进入自动迭代。",
                    trigger_signal_ids=tuple(claim.supporting_signal_ids),
                )
            )
            for signal_id in claim.supporting_signal_ids:
                self.db.add_evidence(
                    claim.claim_id, run_id, signal_id, "support", _weight_for(values, signal_id),
                    "建立基线时纳入的证据",
                )
            for signal_id in claim.contradicting_signal_ids:
                self.db.add_evidence(
                    claim.claim_id, run_id, signal_id, "contra", _weight_for(values, signal_id),
                    "建立基线时记录的相反证据",
                )
            self.db.add_event(
                task["task_id"],
                "claim_baselined",
                {"claim_id": claim.claim_id, "anchor": anchor_key},
                run_id,
            )
            created.append(claim)
        return created

    def _build_baseline_claim(
        self,
        task: dict,
        anchor: Anchor,
        values: list[SignalJudgement],
        signals: dict[str, dict],
        run_id: str,
        now: str,
    ) -> Claim:
        del run_id
        stage = max((v.stage for v in values), default=0)
        stage_label = self.profile.stage_label(stage)
        entities = _entity_names(self.profile, values, anchor)
        statement = anchor.baseline_claim or truncate(
            next((signals[v.signal_id]["title"] for v in values if v.signal_id in signals), anchor.name), 80
        )
        if anchor.baseline_claim and anchor.claim_kind == "technology":
            statement = f"{statement}（当前阶段：{stage_label}）"
        if entities and anchor.baseline_claim and anchor.claim_kind != "technology":
            statement = f"{statement}（主体：{'、'.join(entities[:3])}）"

        supporting = [v.signal_id for v in sorted(values, key=lambda v: v.evidence_strength, reverse=True)[:3]]
        means = sum(v.evidence_strength for v in values) / len(values)
        confidence = clamp(0.30 + 0.35 * means + 0.03 * min(len(values), 6) ** 0.5, 0.28, 0.66)
        source_reliability = _reliability_label(max((v.evidence_strength for v in values), default=0.3))
        impact_confidence = _impact_label(means, len(values))

        open_questions = [
            "该判断的独立来源是否来自不同主体，而不是同一通稿的转载？",
            "是否存在与当前判断相反、但被忽略的证据？",
        ]
        if anchor.question:
            open_questions.insert(0, anchor.question)
        if not any(v.stage >= 2 for v in values):
            open_questions.append("目前证据多停留在论文/样品阶段，距离产线验证还有多远？")

        claim_id = _claim_id(task["task_id"], anchor.key)
        return Claim(
            claim_id=claim_id,
            task_id=task["task_id"],
            anchor_key=anchor.key,
            statement=statement,
            claim_kind=anchor.claim_kind,
            status=ClaimStatus.PROPOSED,
            confidence=round(confidence, 3),
            source_reliability=source_reliability,
            impact_confidence=impact_confidence,
            stage_level=stage,
            high_risk=anchor.high_risk,
            origin="system",
            needs_review=True,
            created_at=now,
            updated_at=now,
            supporting_signal_ids=tuple(supporting),
            contradicting_signal_ids=(),
            open_questions=tuple(open_questions[:4]),
            next_verification=anchor.next_verification,
            watch_indicators=anchor.watch_indicators,
        )

    # ------------------------------------------------------------------ 更新
    def evaluate(
        self,
        task: dict,
        run_id: str,
        judgements: Sequence[SignalJudgement],
        signals: dict[str, dict],
        now: str | None = None,
    ) -> list[Evaluation]:
        """把本期信号映射到已有判断。

        ``signals`` 只包含本期信号；历史支持的“底盘”单独累加，不进入本期增量，
        避免旧证据被反复计分导致置信度虚高。
        """
        now = now or iso_now()
        claims = [
            c
            for c in self.db.list_claims(task["task_id"])
            if c.status
            in {ClaimStatus.PROPOSED, ClaimStatus.ACTIVE, ClaimStatus.WEAKENED, ClaimStatus.FALSIFIED}
        ]
        by_anchor = {c.anchor_key: c for c in claims if c.anchor_key}
        results: list[Evaluation] = []

        buckets: dict[str, ClaimImpact] = {}
        for judgement in judgements:
            if judgement.noise_reason:
                continue
            for anchor_key in judgement.anchors:
                claim = by_anchor.get(anchor_key)
                if claim is None:
                    continue
                bucket = buckets.setdefault(claim.claim_id, ClaimImpact(claim_id=claim.claim_id))
                assessment = self.classifier.evidence_for(judgement)
                assessment.rationale = _join_rationale(judgement)
                direction = self.classifier.direction_for(judgement, claim.claim_kind)
                if direction == 0:
                    bucket.neutral.append(assessment)
                elif assessment.direction < 0:
                    bucket.contra.append(assessment)
                else:
                    bucket.support.append(assessment)

        for claim in claims:
            bucket = buckets.get(claim.claim_id)
            if bucket is None:
                continue
            # 已达成的支持证据也算进当前证据底盘（但不作为“本期变化”的依据）
            for signal_id in claim.supporting_signal_ids:
                if all(a.signal_id != signal_id for a in bucket.support):
                    bucket.support.append(
                        EvidenceAssessment(
                            signal_id=signal_id,
                            direction=1,
                            weight=0.5,
                            in_run=False,
                            rationale="历史支持证据（已计入当前置信度）",
                        )
                    )
            evaluation = self._evaluate_claim(task, claim, bucket, judgements, signals, run_id, now)
            results.append(evaluation)
        return results

    def _evaluate_claim(
        self,
        task: dict,
        claim: Claim,
        bucket: ClaimImpact,
        judgements: Sequence[SignalJudgement],
        signals: dict[str, dict],
        run_id: str,
        now: str,
    ) -> Evaluation:
        if claim.pending_review:
            return Evaluation(
                anchor_key=claim.anchor_key,
                claim=claim,
                kind=ChangeKind.UNRESOLVED,
                status="unresolved",
                reason="上一条修改仍在等待用户确认；本期不再叠加新版本，避免绕过人工复核。",
                support_weight=bucket.support_weight,
                contra_weight=bucket.contra_weight,
                projected_confidence=claim.confidence,
                independent_groups=0,
                support=bucket.new_support,
                contra=bucket.new_contra,
                neutral=bucket.neutral,
                new_signal_ids=sorted(
                    {a.signal_id for a in bucket.new_support + bucket.new_contra}
                ),
                judgements={j.signal_id: j for j in judgements},
            )
        support_weight = bucket.support_weight
        contra_weight = bucket.contra_weight
        new_support_weight = _accumulate([a for a in bucket.support if a.in_run])
        new_contra_weight = _accumulate([a for a in bucket.contra if a.in_run])
        prior_support = _accumulate([a for a in bucket.support if not a.in_run])
        support_ids = {a.signal_id for a in bucket.new_support}
        contra_ids = {a.signal_id for a in bucket.new_contra}
        # 独立来源组只统计“本期新增证据”，历史证据不参与变化说明
        groups = _independent_groups(self.profile, [*support_ids, *contra_ids], signals)
        # 置信度只由“本期新增证据”推动；历史支持已经体现在当前置信度里，不能重复加分。
        # 上限 0.88：判断始终保留“可能错”的空间，避免被推成确定事实。
        projected = clamp(
            claim.confidence
            + 0.62 * new_support_weight
            - 0.55 * new_contra_weight,
            0.08,
            0.88,
        )
        projected = clamp(
            claim.confidence + max(-self.thresholds.max_down, min(self.thresholds.max_up, projected - claim.confidence)),
            0.08,
            0.88,
        )
        delta = projected - claim.confidence
        has_prior = self.db.has_prior_support(claim.claim_id)
        base = Evaluation(
            anchor_key=claim.anchor_key,
            claim=claim,
            kind=ChangeKind.NO_CHANGE,
            status="ignored",
            reason="没有达到推送门槛的新证据。",
            support_weight=support_weight,
            contra_weight=contra_weight,
            projected_confidence=round(projected, 3),
            independent_groups=groups,
            support=bucket.new_support,
            contra=bucket.new_contra,
            neutral=bucket.neutral,
            new_signal_ids=sorted(support_ids | contra_ids),
            judgements={j.signal_id: j for j in judgements},
        )

        strong_contra = contra_weight >= self.thresholds.falsify_contra or (
            _has_explicit_denial(self.profile, contra_ids, signals) and contra_weight >= 0.4
        )
        covered = groups >= self.thresholds.min_independent_groups and not _single_source_trap(
            self.profile, contra_ids, signals
        )
        explicit_denial = _has_explicit_denial(self.profile, contra_ids, signals)

        if strong_contra and (covered or explicit_denial):
            base.kind = ChangeKind.FALSIFY
            base.projected_confidence = clamp(min(claim.confidence, 0.2), 0.05, 0.3)
            base.reason = (
                f"出现 {len(bucket.new_contra)} 条高权重相反证据（合计 {contra_weight:.2f}），"
                f"覆盖 {groups} 个独立来源"
                + ("，且包含来自当事方的明确澄清" if explicit_denial else "")
                + "；当前判断被足够可靠的材料否定。"
            )
            if not covered:
                base.reason += " 注意：目前仍只有单一来源组，结论需独立来源复核。"
            base.status = "applied"
        elif contra_weight >= 0.3 and _contradicts_stage(claim, bucket, signals):
            base.kind = ChangeKind.SHIFT
            base.reason = (
                f"相反证据指向的阶段（S{_max_stage(bucket, signals)}）低于当前判断（S{claim.stage_level}），"
                "系统下调阶段并改写判断。"
            )
            base.status = "applied"
        elif delta <= -self.thresholds.weaken_min:
            base.kind = ChangeKind.WEAKEN
            base.reason = (
                f"新增 {len(bucket.new_contra)} 条相反证据（合计 {contra_weight:.2f}），"
                f"支持证据不足以抵消，置信度由 {claim.confidence:.2f} 降至 {projected:.2f}。"
            )
            base.status = "applied"
        elif _stage_advanced(claim, bucket, signals) and claim.claim_kind in {
            "technology", "roadmap", "standard"
        }:
            # 阶段跃迁优先于置信度微调：这是定性变化，应改写判断而不是只挪数字
            base.kind = ChangeKind.SHIFT
            base.reason = (
                f"证据显示阶段由 S{claim.stage_level} 推进到 S{_max_stage(bucket, signals)}，"
                "判断需要按新阶段改写。"
            )
            base.status = "applied"
        elif delta >= self.thresholds.strengthen_min:
            if not has_prior and not covered:
                base.kind = ChangeKind.NEW
                base.reason = "首次出现可核验的新证据，但尚缺少独立来源，先记录不升级。"
                base.status = "unresolved"
            else:
                base.kind = ChangeKind.STRENGTHEN
                lead = _lead_signal(base, signals)
                lead_title = truncate(signals[lead]["title"], 46) if lead else ""
                base.reason = (
                    f"新增 {len(bucket.new_support)} 条支持证据（合计 {new_support_weight:.2f}，"
                    f"覆盖 {groups} 个独立来源），置信度由 {claim.confidence:.2f} 升至 {projected:.2f}"
                    + (f"；主要依据：{lead_title}" if lead_title else "")
                    + "。"
                )
                base.status = "applied"
        elif bucket.has_new_evidence:
            base.kind = ChangeKind.NO_CHANGE
            base.reason = (
                f"本期有 {len(base.new_signal_ids)} 条相关信号，但证据增量（{delta:+.2f}）"
                "低于变更门槛，判断维持不变。"
            )
            base.status = "ignored"
        else:
            return base

        needs_confirmation = self._requires_confirmation(claim, base, groups)
        if needs_confirmation and base.status == "applied":
            base.status = "pending"

        if base.status in {"applied", "pending"}:
            self._apply(task, claim, base, run_id, now, signals, needs_confirmation)
        return base

    def _requires_confirmation(self, claim: Claim, evaluation: Evaluation, groups: int) -> bool:
        threshold = float(self.db.get_task(claim.task_id).get("high_risk_threshold", 0.6))  # type: ignore[union-attr]
        if evaluation.kind is ChangeKind.FALSIFY:
            return True
        if claim.high_risk and self.thresholds.high_risk_review:
            return True
        if claim.confidence < threshold and evaluation.kind is ChangeKind.STRENGTHEN:
            return True
        return groups < self.thresholds.min_independent_groups

    def _apply(
        self,
        task: dict,
        claim: Claim,
        evaluation: Evaluation,
        run_id: str,
        now: str,
        signals: dict[str, dict],
        pending: bool,
    ) -> None:
        kind = evaluation.kind
        statement = claim.statement
        stage = claim.stage_level
        status = claim.status
        supporting = list(claim.supporting_signal_ids)
        contradicting = list(claim.contradicting_signal_ids)
        confidence = evaluation.projected_confidence

        if kind is ChangeKind.STRENGTHEN:
            status = ClaimStatus.ACTIVE
            stage = max(stage, _max_stage(_bucket_from(evaluation), signals))
        elif kind is ChangeKind.WEAKEN:
            status = ClaimStatus.WEAKENED
            confidence = max(confidence, 0.12)
            # 削弱不自动降阶段：阶段改写需要明确证据，避免“信心下降 = 进度回退”
        elif kind is ChangeKind.FALSIFY:
            status = ClaimStatus.FALSIFIED
            statement = _falsified_statement(claim, evaluation, signals)
            confidence = min(confidence, 0.2)
        elif kind is ChangeKind.SHIFT:
            status = ClaimStatus.ACTIVE
            advanced = _max_stage(_bucket_from(evaluation), signals)
            stage = max(stage, advanced)
            statement = _shift_statement(claim, stage, self.profile)

        supporting.extend(a.signal_id for a in evaluation.support if a.signal_id not in supporting)
        contradicting.extend(a.signal_id for a in evaluation.contra if a.signal_id not in contradicting)
        # 证伪之后，相反证据才是“当前依据”；支持性旧证据保留在历史里但不计入支持
        if kind is ChangeKind.FALSIFY:
            supporting = [sid for sid in supporting if sid not in contradicting]

        version = self.db.latest_version_number(claim.claim_id) + 1
        reason = evaluation.reason
        if pending:
            reason = f"{reason}（高风险或独立来源不足，等待用户确认）"
        self.db.add_claim_version(
            ClaimVersion(
                claim_id=claim.claim_id,
                version=version,
                statement=statement,
                status=status.value,
                confidence=round(confidence, 3),
                stage_level=stage,
                changed_at=now,
                change_kind=kind.value,
                reason=reason,
                trigger_signal_ids=tuple(evaluation.new_signal_ids),
                user_correction="pending" if pending else "",
            )
        )
        for assessment in evaluation.support + evaluation.contra:
            direction = "support" if assessment in evaluation.support else "contra"
            self.db.add_evidence(
                claim.claim_id, run_id, assessment.signal_id, direction,
                assessment.weight, assessment.rationale,
            )
        if kind is ChangeKind.FALSIFY:
            # 被否定的判断：旧的支持证据不再作为“当前依据”计入台账
            stale = [sid for sid in supporting if sid not in contradicting]
            self.db.remove_evidence(claim.claim_id, stale)

        if not pending:
            claim.statement = statement
            claim.status = status
            claim.confidence = round(confidence, 3)
            claim.stage_level = stage
            claim.supporting_signal_ids = tuple(supporting)
            claim.contradicting_signal_ids = tuple(contradicting)
            claim.needs_review = False
            claim.pending_review = False
            claim.confirmed_at = claim.confirmed_at or now
            claim.updated_at = now
            claim.version = version
            self.db.upsert_claim(claim)
        else:
            claim.pending_review = True
            claim.updated_at = now
            self.db.upsert_claim(claim)

        anchor = next((a for a in self.profile.anchors if a.key == claim.anchor_key), None)
        change = self._build_change(task, claim, evaluation, signals, stage, statement, pending, version, anchor)
        evaluation.change = change
        evaluation.version = version
        self.db.add_event(
            task["task_id"],
            "claim_change",
            {
                "claim_id": claim.claim_id,
                "kind": kind.value,
                "status": evaluation.status,
                "version": version,
            },
            run_id,
        )

    # ------------------------------------------------------------------ 变更卡
    def _build_change(
        self,
        task: dict,
        claim: Claim,
        evaluation: Evaluation,
        signals: dict[str, dict],
        stage_after: int,
        statement_after: str,
        pending: bool,
        version: int,
        anchor: Anchor | None,
    ) -> Change:
        kind = evaluation.kind
        lead = _lead_signal(evaluation, signals)
        lead_title = truncate(signals[lead]["title"], 70) if lead else ""
        changed_ids = {
            a.signal_id
            for a in [*evaluation.support, *evaluation.contra]
            if a.signal_id in signals
        }
        source_names = _source_names(self.profile, changed_ids, signals)
        # 每条变化只展示“本期新增”的证据来源数
        source_names = [name for name in source_names if name][:4]

        headline_map = {
            ChangeKind.STRENGTHEN: "获得新的独立证据支持",
            ChangeKind.WEAKEN: "出现反向证据，置信度下调",
            ChangeKind.FALSIFY: "被更可靠的证据否定",
            ChangeKind.SHIFT: "阶段或表述需要改写",
            ChangeKind.NEW: "首次出现值得跟踪的信号",
        }
        headline = headline_map.get(kind, "判断更新")
        if lead_title:
            headline = f"{headline}：{lead_title}"

        delta = evaluation.projected_confidence - claim.confidence
        novelty = _mean_novelty(evaluation, signals)
        lead_judgement = evaluation.judgements.get(lead or "")
        relevance = (
            self.classifier.relevance_to_task(lead_judgement, task)
            if lead_judgement
            else self.classifier.relevance_to_task(_placeholder_judgement(claim.anchor_key), task)
        )
        relevance = max(relevance, 0.5)
        impact = round(
            min(1.0, abs(delta) * 2.6 + evaluation.contra_weight * 0.45 + 0.18 * novelty + 0.12 * relevance),
            3,
        )
        stage_before = claim.stage_level
        stage_text = ""
        if stage_after != stage_before:
            before_label = _stage_label_for(
                claim.claim_kind, stage_before, self.profile.stage_label(stage_before)
            )
            after_label = _stage_label_for(
                claim.claim_kind, stage_after, self.profile.stage_label(stage_after)
            )
            stage_text = f"阶段由 {before_label} 变为 {after_label}。"

        why_changed = " ".join(
            part
            for part in [
                evaluation.reason,
                stage_text,
                f"来源：{'、'.join(source_names[:3])}。" if source_names else "",
            ]
            if part
        )
        why_matters = _why_matters(claim, task, anchor, kind)
        relevance_text = _relevance_text(claim, task, anchor, kind, delta)
        next_steps = tuple(
            [
                *(anchor.watch_indicators if anchor else ()),
                *([anchor.next_verification] if anchor and anchor.next_verification else []),
            ][:4]
        ) or ("等待下一次独立来源确认。",)

        open_questions = list(claim.open_questions)
        if pending:
            open_questions.insert(0, "本条变化尚未确认：需要用户核对证据后才会写入当前判断")
        open_questions = open_questions[:4]
        change = Change(
            change_id=f"chg_{claim.claim_id.split('_')[-1]}_{version}",
            claim_id=claim.claim_id,
            claim_statement=statement_after,
            kind=kind,
            confidence_before=claim.confidence,
            confidence_after=round(evaluation.projected_confidence, 3),
            stage_before=stage_before,
            stage_after=stage_after,
            score=impact,
            headline=headline,
            why_changed=why_changed,
            why_matters=why_matters,
            relevance_to_user=relevance_text,
            next_steps=next_steps,
            open_questions=tuple(open_questions[:4]),
            supporting=tuple(
                _to_judgement(a, claim, signals, "support") for a in evaluation.support if a.signal_id in signals
            ),
            contradicting=tuple(
                _to_judgement(a, claim, signals, "contra")
                for a in evaluation.contra
                if a.signal_id in signals
            ),
            high_risk=claim.high_risk,
            requires_confirmation=pending,
            version_after=version,
        )
        return change

    # ------------------------------------------------------------------ 确认
    def confirm(self, claim_id: str, note: str = "", now: str | None = None) -> Claim:
        claim = self.db.get_claim(claim_id)
        if claim is None:
            raise KeyError(f"判断不存在: {claim_id}")
        timestamp = now or iso_now()
        pending = self.db.pending_version(claim_id)
        if pending is None:
            # 基线判断：没有“版本 2”，确认动作本身就是把它从 proposed 提升为 active
            if claim.status is not ClaimStatus.PROPOSED:
                raise ValueError("没有待确认的修改。")
            claim.status = ClaimStatus.ACTIVE
            claim.needs_review = False
            claim.pending_review = False
            claim.confirmed_at = timestamp
            claim.updated_at = timestamp
            self.db.upsert_claim(claim)
            self.db.add_event(
                claim.task_id, "claim_confirmed", {"claim_id": claim_id, "version": 1}, ""
            )
            return claim
        claim.statement = pending.statement
        claim.status = ClaimStatus(pending.status)
        claim.confidence = pending.confidence
        claim.stage_level = pending.stage_level
        claim.confirmed_at = timestamp
        claim.updated_at = timestamp
        claim.needs_review = False
        claim.pending_review = False
        claim.version = pending.version
        self.db.upsert_claim(claim)
        self.db.delete_claim_version(claim_id, pending.version)
        self.db.add_claim_version(
            ClaimVersion(
                claim_id=claim_id,
                version=pending.version,
                statement=pending.statement,
                status=pending.status,
                confidence=pending.confidence,
                stage_level=pending.stage_level,
                changed_at=pending.changed_at,
                change_kind=pending.change_kind,
                reason=f"{pending.reason} 用户于 {timestamp} 确认。{note}".strip(),
                trigger_signal_ids=pending.trigger_signal_ids,
                user_correction="confirmed",
            )
        )
        self.db.add_event(claim.task_id, "claim_confirmed", {"claim_id": claim_id, "version": pending.version})
        return claim

    def reject(self, claim_id: str, note: str = "") -> Claim:
        claim = self.db.get_claim(claim_id)
        if claim is None:
            raise KeyError(f"判断不存在: {claim_id}")
        pending = self.db.pending_version(claim_id)
        if pending is None:
            raise ValueError("没有待确认的修改。")
        self.db.delete_claim_version(claim_id, pending.version)
        claim.pending_review = False
        claim.updated_at = iso_now()
        self.db.upsert_claim(claim)
        self.db.add_event(
            claim.task_id,
            "claim_rejected",
            {"claim_id": claim_id, "version": pending.version, "note": note},
        )
        return claim

    def rollback(self, claim_id: str, target_version: int, note: str = "") -> Claim:
        """回到某个历史版本，并记录回滚原因（不删除中间版本）。"""
        claim = self.db.get_claim(claim_id)
        if claim is None:
            raise KeyError(f"判断不存在: {claim_id}")
        target = self.db.get_claim_version(claim_id, target_version)
        if target is None:
            raise KeyError(f"版本不存在: {claim_id} v{target_version}")
        new_version = self.db.latest_version_number(claim_id) + 1
        claim.statement = target.statement
        claim.status = ClaimStatus(target.status)
        claim.confidence = target.confidence
        claim.stage_level = target.stage_level
        claim.pending_review = False
        claim.updated_at = iso_now()
        claim.version = new_version
        self.db.upsert_claim(claim)
        self.db.add_claim_version(
            ClaimVersion(
                claim_id=claim_id,
                version=new_version,
                statement=target.statement,
                status=target.status,
                confidence=target.confidence,
                stage_level=target.stage_level,
                changed_at=iso_now(),
                change_kind="rollback",
                reason=f"回滚到 v{target_version}。{note}".strip(),
                trigger_signal_ids=(),
                user_correction="rollback",
                rolled_back_from=target_version,
            )
        )
        self.db.add_event(
            claim.task_id,
            "claim_rolled_back",
            {"claim_id": claim_id, "from": target_version, "to": new_version, "note": note},
        )
        return claim


# --------------------------------------------------------------------- 辅助


def _claim_id(task_id: str, anchor_key: str) -> str:
    return f"clm_{task_id.split('_')[-1]}_{anchor_key}"


def _entity_names(profile: Profile, values: list[SignalJudgement], anchor: Anchor) -> list[str]:
    allowed = set(anchor.entity_keys)
    names: list[str] = []
    for judgement in values:
        for entity_key in judgement.entities:
            if entity_key not in allowed:
                continue
            entity = next((e for e in profile.entities if e.key == entity_key), None)
            if entity and entity.name not in names:
                names.append(entity.name)
    return names


def _weight_for(values: list[SignalJudgement], signal_id: str) -> float:
    for judgement in values:
        if judgement.signal_id == signal_id:
            return round(judgement.evidence_strength, 3)
    return 0.1


def _independent_groups(profile: Profile, signal_ids: Iterable[str], signals: dict[str, dict]) -> int:
    groups: set[str] = set()
    for signal_id in signal_ids:
        signal = signals.get(signal_id)
        if not signal:
            continue
        source_id = str(signal.get("source_id", ""))
        source = profile.source_by_id(source_id)
        groups.add(source.independent_group if source else source_id)
    return len(groups)


def _single_source_trap(profile: Profile, signal_ids: Iterable[str], signals: dict[str, dict]) -> bool:
    """所有相反证据是否来自同一条通稿/同一来源组。"""
    groups = {
        (profile.source_by_id(str(signals[sid].get("source_id", ""))).independent_group
         if profile.source_by_id(str(signals[sid].get("source_id", "")))
         else signals[sid].get("source_id", ""))
        for sid in signal_ids
        if sid in signals
    }
    return len(groups) <= 1


def _has_explicit_denial(profile: Profile, signal_ids: Iterable[str], signals: dict[str, dict]) -> bool:
    patterns = profile.lexicon.get("denial", [])
    if not patterns:
        return False
    for signal_id in signal_ids:
        signal = signals.get(signal_id)
        if not signal:
            continue
        text = f"{signal.get('title', '')}。{signal.get('excerpt', '')}"
        if any(pattern.lower() in text.lower() for pattern in patterns):
            return True
    return False


def _max_stage(bucket: ClaimImpact, signals: dict[str, dict]) -> int:
    """只让“进展型”信号推动阶段，避免标准/招聘/通告类信息被误读成技术阶段。"""
    progress_categories = {
        "pilot_line", "customer_validation", "mass_production_plan",
        "mass_production_actual", "capacity", "product", "research",
        "standard", "official_announcement",
    }
    stages = [
        int(signals[a.signal_id].get("stage_level", 0))
        for a in bucket.support
        if a.signal_id in signals
        and str(signals[a.signal_id].get("category", "")) in progress_categories
    ]
    return max(stages, default=0)


def _contradicts_stage(claim: Claim, bucket: ClaimImpact, signals: dict[str, dict]) -> bool:
    stages = [
        int(signals[a.signal_id].get("stage_level", 0))
        for a in bucket.new_contra
        if a.signal_id in signals
    ]
    if not stages:
        return False
    return max(stages) + 1 < claim.stage_level


def _stage_advanced(claim: Claim, bucket: ClaimImpact, signals: dict[str, dict]) -> bool:
    return _max_stage(bucket, signals) > claim.stage_level + 1


def _falsified_statement(claim: Claim, evaluation: Evaluation, signals: dict[str, dict]) -> str:
    lead = next(iter(evaluation.contra), None)
    evidence = ""
    if lead and lead.signal_id in signals:
        evidence = truncate(signals[lead.signal_id]["title"], 48)
    core = re.sub(r"（主体：.*?）$", "", claim.statement)
    return f"原判断已被否定：{truncate(core, 46)}" + (
        f"（依据：{evidence}）" if evidence else ""
    )


def _shift_statement(claim: Claim, stage: int, profile: Profile) -> str:
    anchor = next((a for a in profile.anchors if a.key == claim.anchor_key), None)
    stage_label = _stage_label_for(claim.claim_kind, stage, profile.stage_label(stage))
    if anchor and anchor.claim_templates.get(str(stage)):
        return anchor.claim_templates[str(stage)]
    if anchor and anchor.claim_template:
        return anchor.claim_template.format(stage=stage_label)
    return f"{claim.statement}（阶段更新为：{stage_label}）"


_STANDARD_STAGE_LABELS = {
    0: "研究阶段",
    1: "研究阶段",
    2: "草案阶段",
    3: "征求意见",
    4: "征求意见",
    5: "发布实施",
}


def _stage_label_for(claim_kind: str, stage: int, default: str) -> str:
    if claim_kind == "standard":
        return _STANDARD_STAGE_LABELS.get(stage, default)
    return default


def _lead_signal(evaluation: Evaluation, signals: dict[str, dict]) -> str | None:
    pool = evaluation.contra if evaluation.kind in {ChangeKind.FALSIFY, ChangeKind.WEAKEN} else evaluation.support
    pool = [a for a in pool if a.signal_id in signals]
    if not pool:
        return None
    return max(pool, key=lambda a: a.weight).signal_id


def _mean_novelty(evaluation: Evaluation, signals: dict[str, dict]) -> float:
    values = [
        float(signals[a.signal_id].get("novelty", 0.3))
        for a in [*evaluation.support, *evaluation.contra]
        if a.signal_id in signals
    ]
    return sum(values) / len(values) if values else 0.3


def _source_names(profile: Profile, signal_ids: Iterable[str], signals: dict[str, dict]) -> list[str]:
    names: list[str] = []
    for signal_id in signal_ids:
        signal = signals.get(signal_id)
        if not signal:
            continue
        source = profile.source_by_id(str(signal.get("source_id", "")))
        name = source.name if source else str(signal.get("source_id", ""))
        if name not in names:
            names.append(name)
    return names


def _to_judgement(
    assessment: EvidenceAssessment, claim: Claim, signals: dict[str, dict], direction: str
) -> SignalJudgement:
    from .models import SignalJudgement

    signal = signals.get(assessment.signal_id, {})
    return SignalJudgement(
        signal_id=assessment.signal_id,
        category=str(signal.get("category", "")),
        info_layer=InfoLayer(str(signal.get("info_layer", "fact"))),
        stage=int(signal.get("stage_level", 0)),
        entities=tuple(signal.get("entities", [])),
        anchors=tuple(signal.get("anchors", [])),
        novelty=float(signal.get("novelty", 0)),
        evidence_strength=assessment.weight,
        reliability=str(signal.get("reliability", "中")),
        relevance=0.0,
        noise_reason="",
        polarity=1 if direction == "support" else -1,
        url=str(signal.get("url", "")),
        excerpt=str(signal.get("excerpt", "")),
        title=str(signal.get("title", "")),
        notes=[assessment.rationale],
    )


def _reliability_label(strength: float) -> str:
    if strength >= 0.62:
        return "高"
    if strength >= 0.42:
        return "中"
    return "低"


def _impact_label(strength: float, count: int) -> str:
    if strength >= 0.6 and count >= 2:
        return "中"
    return "低"


def _join_rationale(judgement: SignalJudgement) -> str:
    return "；".join(judgement.notes[:4])


def _placeholder_judgement(anchor_key: str) -> SignalJudgement:
    return SignalJudgement(
        signal_id="",
        category="media_report",
        info_layer=InfoLayer.OPINION,
        stage=0,
        entities=(),
        anchors=(anchor_key,),
        novelty=0.3,
        evidence_strength=0.3,
        reliability="低",
        relevance=0.0,
        noise_reason="",
    )


def _bucket_from(evaluation: Evaluation) -> ClaimImpact:
    return ClaimImpact(claim_id="", support=evaluation.support, contra=evaluation.contra)


def _why_matters(claim: Claim, task: dict, anchor: Anchor | None, kind: ChangeKind) -> str:
    decision = task.get("decision", "当前决策")
    constraints = task.get("constraints", "")
    direction = {
        ChangeKind.STRENGTHEN: "提高了该判断的可靠性，可以把它当作更稳的前提",
        ChangeKind.WEAKEN: "降低了该判断的可靠性，现在不宜把它当作既定前提",
        ChangeKind.FALSIFY: "推翻了原先的前提，相关计划需要重新评估",
        ChangeKind.SHIFT: "改变了判断的阶段或表述，原有结论的适用范围需要调整",
        ChangeKind.NEW: "提示出现新的跟踪对象，但证据还不足以改变结论",
    }[kind]
    base = f"它{direction}，会直接影响“{decision}”的证据基础。"
    if claim.high_risk:
        base += " 这是一条高风险判断：一旦误判，代价高且难以快速纠正，因此系统提高了更新门槛。"
    if constraints:
        base += f" 你当前的约束是“{constraints}”，本次变化中与该约束直接相关的部分应优先核对。"
    return base


def _relevance_text(claim: Claim, task: dict, anchor: Anchor | None, kind: ChangeKind, delta: float) -> str:
    role = task.get("role", "关注者")
    purpose = task.get("purpose", "")
    horizon = task.get("horizon", "")
    stage = claim.stage_label
    if kind is ChangeKind.FALSIFY:
        opening = (
            f"作为{role}，你正在{purpose or '跟踪这个主题'}，"
            f"这次证伪意味着不能再用“{claim.statement}”作为前提：需要重新找证据，而不是继续加固旧结论。"
        )
    elif kind is ChangeKind.WEAKEN:
        opening = (
            f"作为{role}，你正在{purpose or '跟踪这个主题'}，"
            f"这条判断当前处于 {stage}，本次{delta:+.2f} 的变化削弱了它，相关计划需要留出回旋空间。"
        )
    elif kind is ChangeKind.SHIFT:
        opening = (
            f"作为{role}，你正在{purpose or '跟踪这个主题'}，"
            f"这条判断的阶段或表述发生了变化（当前 {stage}），既有结论的适用范围需要重新界定。"
        )
    else:
        opening = (
            f"作为{role}，你正在{purpose or '跟踪这个主题'}，"
            f"这条判断当前处于 {stage}，本次{delta:+.2f} 的变化{'支持' if delta >= 0 else '削弱'}了它。"
        )
    pieces = [opening]
    if horizon:
        pieces.append(f"你的决策窗口是“{horizon}”，因此需要判断这次变化是否足以改变时间表。")
    if anchor and anchor.question:
        pieces.append(f"它对应你关心的验证问题：{anchor.question}")
    if kind is ChangeKind.FALSIFY:
        pieces.append("建议把它从“既定前提”降级为“待重建假设”。")
    return " ".join(pieces)


def _reliability_for(claim: Claim) -> str:
    return confidence_label(claim.confidence)


def change_score_100(change: Change) -> int:
    """把 0—1 的影响分换算成展示用的 0—100。"""
    return int(round(change.score * 100))
