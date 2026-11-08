"""核心数据模型：信号、证据、判断、变化、报告。

所有对象都是普通 dataclass，便于序列化进 SQLite / JSON，也便于测试直接构造。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

try:  # Python 3.11+
    from enum import StrEnum
except ImportError:  # Python 3.10 兼容：等价的 str 枚举
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """最小兼容实现，行为与 3.11 的 ``enum.StrEnum`` 一致。"""

        def __str__(self) -> str:
            return str(self.value)


class InfoLayer(StrEnum):
    """信息性质分层。事实、口径、推断、观点和传闻不能混写。"""

    FACT = "fact"
    COMPANY_CLAIM = "company_claim"
    INFERENCE = "inference"
    OPINION = "opinion"
    RUMOR = "rumor"
    VERSION = "version"


INFO_LAYER_LABELS: dict[InfoLayer, str] = {
    InfoLayer.FACT: "已确认事实",
    InfoLayer.COMPANY_CLAIM: "公司/机构口径",
    InfoLayer.INFERENCE: "分析推断",
    InfoLayer.OPINION: "行业观点",
    InfoLayer.RUMOR: "未证实传闻",
    InfoLayer.VERSION: "版本记录",
}

INFO_LAYER_STRENGTH: dict[InfoLayer, float] = {
    InfoLayer.FACT: 0.92,
    InfoLayer.COMPANY_CLAIM: 0.74,
    InfoLayer.INFERENCE: 0.45,
    InfoLayer.OPINION: 0.32,
    InfoLayer.RUMOR: 0.16,
    InfoLayer.VERSION: 0.7,
}


class ChangeKind(StrEnum):
    NEW = "new"
    STRENGTHEN = "strengthen"
    WEAKEN = "weaken"
    FALSIFY = "falsify"
    SHIFT = "shift"
    NO_CHANGE = "no_change"
    UNRESOLVED = "unresolved"


CHANGE_LABELS: dict[ChangeKind, str] = {
    ChangeKind.NEW: "新增",
    ChangeKind.STRENGTHEN: "增强",
    ChangeKind.WEAKEN: "削弱",
    ChangeKind.FALSIFY: "证伪",
    ChangeKind.SHIFT: "转向",
    ChangeKind.NO_CHANGE: "无实质变化",
    ChangeKind.UNRESOLVED: "证据不足，暂不更新",
}


class ClaimStatus(StrEnum):
    PROPOSED = "proposed"      # 系统提出，尚未被确认（不参与后续自动迭代）
    ACTIVE = "active"          # 已确认，纳入持续追踪
    WEAKENED = "weakened"      # 已被削弱，仍在追踪
    FALSIFIED = "falsified"    # 已被证伪，保留历史
    RETIRED = "retired"        # 用户主动停用
    ROLLED_BACK = "rolled_back"


class ClaimKind(StrEnum):
    TECHNOLOGY = "technology"
    COMMERCIALIZATION = "commercialization"
    ROADMAP = "roadmap"
    STANDARD = "standard"
    TALENT = "talent"
    MARKET = "market"


@dataclass(slots=True)
class Signal:
    """一条被采集到的原始信息（未去重、未分类）。"""

    signal_id: str
    source_id: str
    title: str
    url: str
    published_at: str  # ISO8601，来源发布时间
    retrieved_at: str
    excerpt: str = ""
    author: str = ""
    lang: str = "zh"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SignalRecord:
    """落库后带分类结果的信号。"""

    signal_id: str
    run_id: str
    source_id: str
    title: str
    url: str
    url_key: str
    published_at: str
    retrieved_at: str
    excerpt: str
    category: str
    info_layer: InfoLayer
    stage_level: int
    stage_label: str
    entities: tuple[str, ...]
    anchors: tuple[str, ...]
    novelty: float
    noise_reason: str
    duplicate_of: str | None
    canonical_group: str
    content_hash: str
    reliability: str = "medium"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_noise(self) -> bool:
        return bool(self.noise_reason)

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


@dataclass(slots=True)
class SignalJudgement:
    """系统对单条信号的可解释判断。"""

    signal_id: str
    category: str
    info_layer: InfoLayer
    stage: int
    entities: tuple[str, ...]
    anchors: tuple[str, ...]
    novelty: float
    evidence_strength: float
    reliability: str
    relevance: float
    noise_reason: str
    polarity: int = 1
    url: str = ""
    excerpt: str = ""
    title: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Claim:
    claim_id: str
    task_id: str
    anchor_key: str
    statement: str
    claim_kind: str
    status: ClaimStatus
    confidence: float
    source_reliability: str
    impact_confidence: str
    stage_level: int
    high_risk: bool
    origin: str  # system / user / llm_draft
    needs_review: bool
    created_at: str
    updated_at: str
    confirmed_at: str | None = None
    pending_review: bool = False
    supporting_signal_ids: tuple[str, ...] = ()
    contradicting_signal_ids: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    next_verification: str = ""
    watch_indicators: tuple[str, ...] = ()
    version: int = 1

    @property
    def stage_label(self) -> str:
        return f"S{self.stage_level}"


@dataclass(slots=True)
class ClaimVersion:
    claim_id: str
    version: int
    statement: str
    status: str
    confidence: float
    stage_level: int
    changed_at: str
    change_kind: str
    reason: str
    trigger_signal_ids: tuple[str, ...] = ()
    user_correction: str = ""
    rolled_back_from: int | None = None


@dataclass(slots=True)
class Change:
    change_id: str
    claim_id: str
    claim_statement: str
    kind: ChangeKind
    confidence_before: float
    confidence_after: float
    stage_before: int
    stage_after: int
    score: float
    headline: str
    why_changed: str
    why_matters: str
    relevance_to_user: str
    next_steps: tuple[str, ...]
    open_questions: tuple[str, ...]
    supporting: tuple[SignalJudgement, ...] = ()
    contradicting: tuple[SignalJudgement, ...] = ()
    high_risk: bool = False
    requires_confirmation: bool = False
    version_after: int = 0

    @property
    def confidence_delta(self) -> float:
        return round(self.confidence_after - self.confidence_before, 3)


@dataclass(slots=True)
class Digest:
    run_id: str
    task_id: str
    run_at: str
    period_start: str
    period_end: str
    mode: str  # baseline / update / quiet
    summary: dict[str, Any]
    changes: list[Change]
    timeline: list[dict[str, Any]]
    not_pushed: list[dict[str, Any]]
    coverage: dict[str, Any]
    relevance: dict[str, Any]
    metrics: dict[str, Any]
    claims: list[dict[str, Any]] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def _jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        return round(value, 4)
    return value


def dumps(value: Any, indent: int = 2) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, indent=indent)


def parse_dt(value: str) -> str:
    """把常见时间写法规范成 ISO8601 字符串（保留时区）。"""
    from datetime import datetime, timezone

    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return text
