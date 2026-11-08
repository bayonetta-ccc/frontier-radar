"""领域配置（domain profile）加载与访问。

一个 profile 描述“某个主题世界里什么算信号”：

* ``lexicon``  —— 类别词表、阶段词表、噪声词表、信息分层词表；
* ``entities`` —— 需要追踪的主体（公司、机构、路线、产品）；
* ``anchors``  —— 判断锚点：把信号聚合成可检验的主张；
* ``stages``   —— 技术/产业阶段刻度，防止越级推断；
* ``sources``  —— 固定核心来源清单（离线 fixture 或在线拉取）。

profile 是数据而不是代码，新增领域只需新增一个 JSON 文件。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PROFILE = Path(__file__).resolve().parent.parent / "config" / "domains"


class ProfileError(ValueError):
    """profile 文件缺失或结构非法。"""


@dataclass(frozen=True)
class Entity:
    key: str
    name: str
    aliases: tuple[str, ...] = ()
    kind: str = "org"

    def tokens(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class Anchor:
    key: str
    name: str
    claim_kind: str  # technology / commercialization / roadmap / standard / talent
    keywords: tuple[str, ...] = ()
    entity_keys: tuple[str, ...] = ()
    question: str = ""
    high_risk: bool = False
    watch_indicators: tuple[str, ...] = ()
    next_verification: str = ""
    baseline_claim: str = ""
    claim_template: str = ""
    create_baseline_claim: bool = True


@dataclass(frozen=True)
class Stage:
    level: int
    key: str
    label: str
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceDef:
    id: str
    name: str
    kind: str  # official / paper / patent / standard / repository / job / media / social
    authority: str  # high / medium / low
    cadence: str = "weekly"
    url: str = ""
    offline_file: str = ""
    note: str = ""
    independent_group: str = ""


@dataclass
class Profile:
    slug: str
    name: str
    language: str
    description: str
    stages: list[Stage]
    entities: list[Entity]
    anchors: list[Anchor]
    sources: list[SourceDef]
    lexicon: dict[str, list[str]]
    info_layers: dict[str, list[str]]
    negation_phrases: list[str] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
    gap_questions: list[str] = field(default_factory=list)
    noise: dict[str, Any] = field(default_factory=dict)
    category_blueprint: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stage_patterns: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    # ---- 访问器 -----------------------------------------------------------
    def stage_label(self, level: int) -> str:
        for stage in self.stages:
            if stage.level == level:
                return stage.label
        return "未判定阶段"

    def stage_by_key(self, key: str) -> Stage | None:
        for stage in self.stages:
            if stage.key == key:
                return stage
        return None

    def entity_aliases(self) -> list[tuple[str, Entity]]:
        pairs: list[tuple[str, Entity]] = []
        for entity in self.entities:
            for token in entity.tokens():
                if token:
                    pairs.append((token, entity))
        # 长别名优先，避免“宁德时代新能源”被“宁德时代”抢先匹配
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        return pairs

    def anchors_for(self, entity_keys: set[str], claim_kind: str | None = None) -> list[Anchor]:
        hits = [a for a in self.anchors if entity_keys & set(a.entity_keys)]
        if claim_kind:
            narrowed = [a for a in hits if a.claim_kind == claim_kind]
            if narrowed:
                return narrowed
        return hits

    def source_by_id(self, source_id: str) -> SourceDef | None:
        for source in self.sources:
            if source.id == source_id:
                return source
        return None

    def source_kind(self, source_id: str) -> str:
        source = self.source_by_id(source_id)
        return source.kind if source else "unknown"

    def source_authority(self, source_id: str) -> str:
        source = self.source_by_id(source_id)
        return source.authority if source else "low"


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def load_profile(path_or_slug: str | Path, base_dir: Path | None = None) -> Profile:
    """按文件路径或 profile 名称加载。"""
    base = Path(base_dir) if base_dir else DEFAULT_PROFILE
    candidate = Path(path_or_slug)
    if not candidate.exists():
        candidate = base / f"{path_or_slug}.json"
    if not candidate.exists():
        available = ", ".join(sorted(p.stem for p in base.glob("*.json")))
        raise ProfileError(f"找不到领域配置 {path_or_slug!r}；可用: {available}")
    try:
        raw = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:  # pragma: no cover - 配置错误提示
        raise ProfileError(f"领域配置不是合法 JSON: {candidate} ({exc})") from exc

    stages = [
        Stage(
            level=int(item["level"]),
            key=str(item["key"]),
            label=str(item["label"]),
            keywords=_as_tuple(item.get("keywords")),
        )
        for item in raw.get("stages", [])
    ]
    stages.sort(key=lambda s: s.level)
    entities = [
        Entity(
            key=str(item["key"]),
            name=str(item["name"]),
            aliases=_as_tuple(item.get("aliases")),
            kind=str(item.get("kind", "org")),
        )
        for item in raw.get("entities", [])
    ]
    anchors = [
        Anchor(
            key=str(item["key"]),
            name=str(item["name"]),
            claim_kind=str(item.get("claim_kind", "technology")),
            keywords=_as_tuple(item.get("keywords")),
            entity_keys=_as_tuple(item.get("entity_keys")),
            question=str(item.get("question", "")),
            high_risk=bool(item.get("high_risk", False)),
            watch_indicators=_as_tuple(item.get("watch_indicators")),
            next_verification=str(item.get("next_verification", "")),
            baseline_claim=str(item.get("baseline_claim", "")),
            claim_template=str(item.get("claim_template", "")),
            create_baseline_claim=bool(item.get("create_baseline_claim", True)),
        )
        for item in raw.get("anchors", [])
    ]
    sources = [
        SourceDef(
            id=str(item["id"]),
            name=str(item["name"]),
            kind=str(item.get("kind", "media")),
            authority=str(item.get("authority", "medium")),
            cadence=str(item.get("cadence", "weekly")),
            url=str(item.get("url", "")),
            offline_file=str(item.get("offline_file", "")),
            note=str(item.get("note", "")),
            independent_group=str(item.get("independent_group", item["id"])),
        )
        for item in raw.get("sources", [])
    ]
    profile = Profile(
        slug=str(raw["slug"]),
        name=str(raw["name"]),
        language=str(raw.get("language", "zh")),
        description=str(raw.get("description", "")),
        stages=stages,
        entities=entities,
        anchors=anchors,
        sources=sources,
        lexicon={k: list(map(str, v)) for k, v in raw.get("lexicon", {}).items()},
        info_layers={k: list(map(str, v)) for k, v in raw.get("info_layers", {}).items()},
        negation_phrases=[str(x) for x in raw.get("negation_phrases", [])],
        coverage_notes=[str(x) for x in raw.get("coverage_notes", [])],
        gap_questions=[str(x) for x in raw.get("gap_questions", [])],
        noise=dict(raw.get("noise", {})),
        category_blueprint={
            str(k): [dict(e) for e in v] for k, v in raw.get("category_blueprint", {}).items()
        },
        stage_patterns={
            str(k): [dict(e) for e in v] for k, v in raw.get("stage_patterns", {}).items()
        },
    )
    if not profile.stages:
        raise ProfileError(f"领域配置缺少 stages: {candidate}")
    return profile


def compile_phrase_pattern(phrases: list[str]) -> re.Pattern[str] | None:
    """把词组列表编译成大小写不敏感的匹配模式。

    支持两种写法：独立词组（``"中试线"``）与正则备选（``"中试|试产"``）。
    备选内部按片段转义，保证配置里的特殊字符不会变成正则元字符。
    """
    prepared: list[str] = []
    for phrase in phrases:
        text = (phrase or "").strip()
        if not text:
            continue
        parts = [re.escape(part.strip()) for part in text.split("|") if part.strip()]
        if parts:
            prepared.append("|".join(parts))
    prepared = sorted(set(prepared), key=len, reverse=True)
    if not prepared:
        return None
    return re.compile("|".join(prepared), re.IGNORECASE)
