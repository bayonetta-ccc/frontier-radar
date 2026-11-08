"""证据层：把原始信号变成可解释的判断素材。

这里刻意保持“规则 + 词表”的确定性实现：
* 无网络、无密钥也能跑，结果可复现、可写测试；
* 每一条打分都能说清依据（notes / rationale），便于审计；
* LLM 只能作为“起草器”接在外部，不能绕过这一层直接改判断。

核心概念：
    evidence_strength = 信息性质 × 来源权威 × 独立印证 × 阶段具体性
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .models import InfoLayer, INFO_LAYER_STRENGTH, Signal, SignalJudgement
from .profile import Anchor, Profile, compile_phrase_pattern
from .text import clean_text, content_hash, jaccard, overlap_ratio, tokens, truncate, url_key

AUTHORITY_STRENGTH = {"high": 0.9, "medium": 0.6, "low": 0.35, "unknown": 0.3}

# 类别 → 该类别默认对应的“阶段”上限，避免把发布会当成量产
CATEGORY_STAGE_CAP = {
    "research": 0,
    "official_announcement": 4,
    "product": 2,
    "patent": 1,
    "standard": 5,
    "pilot_line": 2,
    "capacity": 2,
    "customer_validation": 3,
    "mass_production_plan": 3,
    "mass_production_actual": 5,
    "funding": 2,
    "hiring": 0,
    "open_source": 0,
    "opinion_market": 0,
    "hype_product": 0,
}

CATEGORY_WEIGHT = {
    "mass_production_actual": 1.0,
    "customer_validation": 0.9,
    "mass_production_plan": 0.8,
    "pilot_line": 0.85,
    "capacity": 0.8,
    "standard": 0.8,
    "official_announcement": 0.7,
    "research": 0.75,
    "product": 0.65,
    "patent": 0.6,
    "funding": 0.55,
    "hiring": 0.45,
    "open_source": 0.45,
    "opinion_market": 0.2,
    "hype_product": 0.15,
}


@dataclass
class EvidenceAssessment:
    """单条信号在某个判断上的证据评估。"""

    signal_id: str
    direction: int  # +1 支持 / -1 反对 / 0 中性
    weight: float
    in_run: bool = True
    rationale: str = ""


@dataclass
class ClaimImpact:
    claim_id: str
    support: list[EvidenceAssessment] = field(default_factory=list)
    contra: list[EvidenceAssessment] = field(default_factory=list)
    neutral: list[EvidenceAssessment] = field(default_factory=list)

    @property
    def support_weight(self) -> float:
        return _accumulate([a for a in self.support if a.in_run])

    @property
    def contra_weight(self) -> float:
        return _accumulate([a for a in self.contra if a.in_run])

    @property
    def has_new_evidence(self) -> bool:
        return any(a.in_run for a in self.support + self.contra + self.neutral)

    @property
    def new_support(self) -> list[EvidenceAssessment]:
        return [a for a in self.support if a.in_run]

    @property
    def new_contra(self) -> list[EvidenceAssessment]:
        return [a for a in self.contra if a.in_run]


def _accumulate(items: Iterable[EvidenceAssessment]) -> float:
    """同一方向多条证据叠加，但递减收益并限制上界。"""
    weights = sorted((a.weight for a in items), reverse=True)
    total = 0.0
    for index, weight in enumerate(weights):
        total += weight * (0.68 ** index)
    return min(total, 1.35)


class Classifier:
    def __init__(self, profile: Profile, noise_config: dict | None = None) -> None:
        self.profile = profile
        self._lex = {k: compile_phrase_pattern(v) for k, v in profile.lexicon.items()}
        self._stage_patterns = [
            (stage.level, stage.label, compile_phrase_pattern(list(stage.keywords)))
            for stage in profile.stages
        ]
        self._stage_blueprint: list[tuple[int, str, list[tuple[re.Pattern[str], float]]]] = []
        for stage in profile.stages:
            entries = profile.stage_patterns.get(stage.key, []) if profile.stage_patterns else []
            compiled_entries: list[tuple[re.Pattern[str], float]] = []
            for entry in entries:
                pattern = compile_phrase_pattern([entry["pattern"]])
                if pattern:
                    compiled_entries.append((pattern, float(entry.get("score", 1.0))))
            if compiled_entries:
                self._stage_blueprint.append((stage.level, stage.label, compiled_entries))
        self._category_blueprint: dict[str, list[tuple[re.Pattern[str], float, str]]] = {}
        for category, entries in (profile.category_blueprint or {}).items():
            compiled: list[tuple[re.Pattern[str], float, str]] = []
            for entry in entries:
                pattern = compile_phrase_pattern([entry["pattern"]])
                if pattern:
                    compiled.append((pattern, float(entry.get("score", 0.5)), entry.get("label", "")))
            if compiled:
                self._category_blueprint[category] = compiled
        self._info_patterns = {
            layer: compile_phrase_pattern(phrases) for layer, phrases in profile.info_layers.items()
        }
        self.noise = {"min_excerpt_chars": 24, **(noise_config or profile.noise or {})}
        self._off_topic = compile_phrase_pattern(self._as_list(self.noise.get("off_topic_patterns")))
        self._marketing = compile_phrase_pattern(self._as_list(self.noise.get("marketing_patterns")))
        self._rumor = self._lex.get("rumor")
        self._delay = self._lex.get("delay")
        self._failure = self._lex.get("failure")
        self._denial = self._lex.get("denial")
        self._hype = self._lex.get("hype")
        self._negation = compile_phrase_pattern(self.profile.negation_phrases or [])

    @staticmethod
    def _as_list(value: object) -> list[str]:
        return [str(v) for v in value] if isinstance(value, (list, tuple)) else []

    def _has_customer_evidence(self, text: str) -> bool:
        """判断文本是否包含客户侧/交付侧证据，用于拦截“只有发布会”的信号。

        先剔除含否定词的短句，避免“未披露客户订单”被当成客户证据。
        """
        patterns = self._as_list(self.noise.get("launch_evidence_patterns")) or [
            "订单", "定点", "采购", "中标", "交付", "验收", "产线", "试用", "复购",
        ]
        clauses = re.split(r"[。；;]", text)
        for clause in clauses:
            if re.search(r"(未|没有|尚未|并未|无)", clause):
                continue
            if any(str(p).lower() in clause.lower() for p in patterns):
                return True
        return False

    # ---- 词表匹配 ---------------------------------------------------------
    def _category_scores(self, text: str) -> dict[str, float]:
        """按“锚点短语”给类别打分。

        关键取舍：类别由最有信息量的短语决定，而不是命中次数。
        否则“客户验证”会因正文里出现的“量产”被误判成实际量产。
        """
        scores: dict[str, float] = {}
        if self._category_blueprint:
            for category, entries in self._category_blueprint.items():
                total = 0.0
                for pattern, weight, _label in entries:
                    if pattern.search(text):
                        total += weight
                if total:
                    scores[category] = total
            return scores
        # 兼容没有 blueprint 的旧 profile
        for category, pattern in self._lex.items():
            if category in {"delay", "failure", "denial", "milestone", "hype", "rumor"}:
                continue
            if pattern is None:
                continue
            hits = len(set(m.group(0).lower() for m in pattern.finditer(text)))
            if hits:
                scores[category] = hits * CATEGORY_WEIGHT.get(category, 0.5)
        return scores

    def classify(self, signal: Signal, seen_titles: list[str] | None = None) -> SignalJudgement:
        title = clean_text(signal.title)
        excerpt = clean_text(signal.excerpt)
        text = f"{title}。{excerpt}"
        notes: list[str] = []

        category_scores = self._category_scores(text)
        # 标题决定“这条信息是什么”，正文只做补充：避免正文里的背景词把类别带偏
        title_scores = self._category_scores(title)
        for category_key, value in title_scores.items():
            category_scores[category_key] = category_scores.get(category_key, 0) + value * 2
        category = max(category_scores, key=category_scores.get) if category_scores else "media_report"
        notes.append(f"类别={category}（词表命中 {category_scores.get(category, 0):.2f}）")

        # 营销词密集但没有可核验数据：无论来自什么源，都先降级为营销内容
        marketing_hits = (
            len(set(m.group(0) for m in self._marketing.finditer(text))) if self._marketing else 0
        )
        has_evidence_data = bool(
            re.search(r"\d+(\.\d+)?\s*(%|GWh|Wh/kg|次|亿元|万辆|台|件|元/Wh)", text)
            or re.search(r"(数据|测试|报告|论文|公告|披露|实测|样本)", text)
        )
        if marketing_hits >= 2 and not has_evidence_data:
            category = "hype_product"
            notes.append(f"营销词密集（{marketing_hits} 个）且无可核验数据，降级为营销内容")

        polarity = self.signal_polarity(signal)
        stage = self.detect_stage(text, category)
        if polarity < 0:
            # 延期/失败信号不能被“投产/量产”等词抬高阶段
            stage = min(stage, 2)
            notes.append("负向信号：阶段按已知进展上限处理")
        info_layer = self.detect_info_layer(text, signal)
        entities = self.detect_entities(text)
        anchors = self.detect_anchors(text, entities, category)
        novelty = self.estimate_novelty(text, seen_titles or [])
        noise_reason = self.detect_noise(signal, text, category, info_layer)

        authority = AUTHORITY_STRENGTH.get(self.profile.source_authority(signal.source_id), 0.3)
        strength = (
            INFO_LAYER_STRENGTH[info_layer]
            * (0.5 + 0.5 * authority)
            * (0.7 + 0.3 * CATEGORY_WEIGHT.get(category, 0.5))
            * (0.75 + 0.25 * min(stage, 5) / 5)
        )
        if noise_reason:
            strength *= 0.25
        reliability = "高" if strength >= 0.62 else ("中" if strength >= 0.42 else "低")

        return SignalJudgement(
            signal_id=signal.signal_id,
            category=category,
            info_layer=info_layer,
            stage=stage,
            entities=tuple(e.key for e in entities),
            anchors=tuple(a.key for a in anchors),
            novelty=round(novelty, 3),
            evidence_strength=round(strength, 3),
            reliability=reliability,
            relevance=0.0,
            noise_reason=noise_reason,
            polarity=polarity,
            notes=notes,
        )

    # ---- 各维度 -----------------------------------------------------------
    def detect_stage(self, text: str, category: str = "") -> int:
        best = 0
        if self._stage_blueprint:
            for level, _label, entries in self._stage_blueprint:
                total = 0.0
                for pattern, weight in entries:
                    for match in pattern.finditer(text):
                        if not _is_negated(text, match.start()):
                            total += weight
                            break
                if total >= 1.5:
                    best = max(best, level)
        else:
            for level, _label, pattern in self._stage_patterns:
                if pattern is None:
                    continue
                if pattern.search(text):
                    best = max(best, level)
        cap = CATEGORY_STAGE_CAP.get(category)
        if cap is not None:
            # 类别本身不能被词表抬到更高阶段（例如“量产计划”不能变成“已量产”）
            best = min(best, cap)
        return best

    def detect_info_layer(self, text: str, signal: Signal) -> InfoLayer:
        authority = self.profile.source_authority(signal.source_id)
        kind = self.profile.source_kind(signal.source_id)
        if self._rumor and self._rumor.search(text) and authority != "high":
            return InfoLayer.RUMOR
        if kind == "job":
            return InfoLayer.COMPANY_CLAIM
        hits: dict[InfoLayer, int] = {}
        mapping = {
            "fact": InfoLayer.FACT,
            "company_claim": InfoLayer.COMPANY_CLAIM,
            "inference": InfoLayer.INFERENCE,
            "opinion": InfoLayer.OPINION,
            "rumor": InfoLayer.RUMOR,
            "version": InfoLayer.VERSION,
        }
        for layer_key, pattern in self._info_patterns.items():
            if pattern is None or layer_key not in mapping:
                continue
            hits[mapping[layer_key]] = len(set(m.group(0) for m in pattern.finditer(text)))
        if kind in {"official", "standard"} and hits.get(InfoLayer.COMPANY_CLAIM):
            return InfoLayer.COMPANY_CLAIM
        if kind in {"social"}:
            return InfoLayer.RUMOR
        if hits:
            best = max(hits, key=lambda k: (hits[k], k == InfoLayer.FACT))
            # 高权威来源的事实性描述优先
            if authority == "high" and hits.get(InfoLayer.FACT):
                return InfoLayer.FACT
            return best
        if kind in {"official", "paper", "patent", "standard", "repository"}:
            return InfoLayer.FACT
        if authority == "low":
            return InfoLayer.OPINION
        return InfoLayer.FACT

    def detect_entities(self, text: str) -> list:
        found = []
        used: set[str] = set()
        lowered = text.lower()
        for token, entity in self.profile.entity_aliases():
            if entity.key in used:
                continue
            if token.lower() in lowered:
                used.add(entity.key)
                found.append(entity)
        return found

    def detect_anchors(self, text: str, entities: Iterable, category: str = "") -> list[Anchor]:
        entity_keys = {e.key for e in entities}
        anchor_tokens = tokens(text)
        scored: list[tuple[float, Anchor]] = []
        for anchor in self.profile.anchors:
            matched_entities = entity_keys & set(anchor.entity_keys)
            keyword_hits = sum(1 for keyword in anchor.keywords if keyword.lower() in text.lower())
            # 信号已有明确主体时，必须命中该判断的主体；没有任何实体时，至少命中 2 个关键词
            if entity_keys and not matched_entities:
                continue
            if not entity_keys and keyword_hits < 2:
                continue
            score = 0.0
            if matched_entities:
                score += 2.0
            if keyword_hits:
                score += min(keyword_hits * 0.65, 2.4)
            name_overlap = overlap_ratio(tokens(anchor.name), anchor_tokens)
            score += name_overlap * 1.2
            threshold = 2.0 if entity_keys else 1.2
            if score >= threshold:
                scored.append((score, anchor))
        scored.sort(key=lambda item: item[0], reverse=True)
        # 分数接近时，按“信号类别 → 判断类型”偏好打破平局：
        # 客户验证优先落到商业化判断，产能/专利优先落到技术判断，标准落到标准判断
        preferred_kind = _preferred_claim_kind(category)
        if preferred_kind:
            scored = [
                (score + (0.3 if anchor.claim_kind == preferred_kind else 0.0), anchor)
                for score, anchor in scored
            ]
            scored.sort(key=lambda item: item[0], reverse=True)
        # 只保留最强的一个归属：一条信号同时“支持”多条判断，是最常见的证据污染源
        return [anchor for _score, anchor in scored[:1]]

    def estimate_novelty(self, text: str, seen_titles: list[str]) -> float:
        if not seen_titles:
            return 0.75
        current = tokens(text)
        similarity = max((jaccard(current, tokens(title)) for title in seen_titles), default=0.0)
        return round(max(0.05, 1.0 - similarity), 3)

    def detect_noise(
        self, signal: Signal, text: str, category: str, info_layer: InfoLayer
    ) -> str:
        if not clean_text(signal.title):
            return "空标题"
        if self._off_topic and self._off_topic.search(text):
            return "与追踪主题无关（命中离题词表）"
        excerpt_len = len(clean_text(signal.excerpt))
        if excerpt_len < int(self.noise.get("min_excerpt_chars", 24)):
            return "信息量过低：缺少可核验的原文内容"
        if category in set(self.noise.get("noise_categories", [])):
            return f"营销或观点内容（{category}），没有可核验的新事实"
        # 发布会/产品亮相：没有客户侧证据时，只能算“公司说了”，不能算进展
        launch = re.search(r"(发布会|展会|首秀|亮相|现场演示)", text) is not None
        if (category == "product" or launch) and not self._has_customer_evidence(text):
            return "仅发布会/产品亮相，未提供客户案例、订单或交付证据"
        if info_layer is InfoLayer.RUMOR and self.profile.source_authority(signal.source_id) != "high":
            return "仅匿名/未证实传闻，未达到更新门槛"
        marketing_hits = 0
        if self._marketing:
            marketing_hits = len(set(m.group(0) for m in self._marketing.finditer(text)))
        if marketing_hits >= 1 and (
            category == "hype_product" or info_layer in {InfoLayer.INFERENCE, InfoLayer.OPINION, InfoLayer.RUMOR}
        ):
            return "标题或正文以营销词为主，缺少可核验事实"
        return ""

    # ---- 判断相关 ---------------------------------------------------------
    def evidence_for(self, judgement: SignalJudgement) -> EvidenceAssessment:
        """把分类判断折算为“可叠加的证据权重”。"""
        weight = judgement.evidence_strength * (0.55 + 0.45 * judgement.novelty)
        if judgement.noise_reason:
            weight *= 0.15
        return EvidenceAssessment(
            signal_id=judgement.signal_id,
            direction=judgement.polarity,
            weight=round(min(weight, 0.95), 3),
            rationale="；".join(
                [
                    f"类别={judgement.category}",
                    f"性质={judgement.info_layer.value}",
                    f"阶段=S{judgement.stage}",
                    f"新颖性={judgement.novelty:.2f}",
                    *judgement.notes,
                ]
            ),
        )

    def direction_for(self, judgement: SignalJudgement, claim_kind: str) -> int:
        """判断一条信号的类别是否与判断类型对得上（方向由 polarity 决定）。"""
        if judgement.noise_reason:
            return 0
        category = judgement.category
        if category in {"hype_product", "opinion_market", "product"}:
            # 产品发布/亮相只是“存在这个产品”，不能当作客户验证或量产证据
            return 0
        if category in {"hiring", "funding"}:
            # 招聘与融资是背景信息，不直接支持或削弱一条技术/商业化判断
            return 0
        if judgement.info_layer in {InfoLayer.RUMOR, InfoLayer.OPINION}:
            return 0
        if category == "media_report":
            return 0
        if claim_kind == "talent" and category not in {"hiring"}:
            return 0
        if claim_kind == "standard" and category not in {"standard", "official_announcement"}:
            return 0
        return 1

    def signal_polarity(self, signal: Signal) -> int:
        """信号本身的正负向：延期/失败 = -1；“否认延期”不算反转。"""
        text = f"{signal.title}。{signal.excerpt}"
        denial = bool(self._denial and self._denial.search(text))
        delay = bool(self._delay and self._delay.search(text))
        failure = bool(self._failure and self._failure.search(text))
        explicit_negative = bool(self._denial and self._denial.search(text)) and bool(
            self._negation and self._negation.search(text)
        )
        # “澄清/否认 + 已否定事实”属于反方向陈述，直接判为负向
        if explicit_negative:
            return -1
        if re.search(r"(否认|澄清|不存在)[^。；]{0,12}(推迟|延期|失败|取消|撤回)", text):
            return 1
        if failure or delay:
            return -1
        return 1

    def relevance_to_task(self, judgement: SignalJudgement, task: dict) -> float:
        """用户相关性：命中的主体/锚点是否属于用户关注清单与决策问题。"""
        watchlist = {str(x).lower() for x in task.get("watchlist", [])}
        decision_tokens = tokens(
            " ".join(
                str(task.get(field, ""))
                for field in ("topic", "decision", "constraints", "decision_criteria", "purpose")
            )
        )
        score = 0.35
        if judgement.anchors:
            score += 0.3
        if judgement.stage >= 2:
            score += 0.1
        if watchlist and any(entity.lower() in watchlist for entity in judgement.entities):
            score += 0.15
        text_tokens = set(judgement.anchors) | set(judgement.entities)
        if text_tokens & decision_tokens:
            score += 0.1
        if judgement.noise_reason:
            score *= 0.4
        return round(min(score, 1.0), 3)


def _is_negated(text: str, index: int, window: int = 10) -> bool:
    """判断某个匹配前面是否紧邻否定词，例如“尚未正式发布”。"""
    prefix = text[max(0, index - window):index]
    return re.search(r"(尚未|未|没有|并未|并未曾|不再|否认)$", prefix) is not None or re.search(
        r"(尚未|未|没有|并未)[^，。；]{0,4}$", prefix
    ) is not None


def _preferred_claim_kind(category: str) -> str:
    if category in {"customer_validation", "mass_production_plan", "mass_production_actual"}:
        return "commercialization"
    if category in {"pilot_line", "capacity", "research", "patent", "product"}:
        return "technology"
    if category == "standard":
        return "standard"
    return ""


def dedupe_key(signal: Signal) -> tuple[str, str]:
    return url_key(signal.url), content_hash(signal.title, signal.excerpt)


def is_same_story(a: dict, b: dict, threshold: float = 0.62) -> bool:
    """粗粒度同事件判断：来自同一规范链接，或标题高度重合。"""
    if a.get("url_key") and a.get("url_key") == b.get("url_key"):
        return True
    return jaccard(tokens(a.get("title", "")), tokens(b.get("title", ""))) >= threshold


def excerpt_matches_source(signal: dict, source_text: str) -> bool:
    """原文摘录与来源内容的一致率（用于审计“链接与主张是否对得上”）。"""
    excerpt = clean_text(signal.get("excerpt", ""))
    if not excerpt:
        return False
    haystack = clean_text(source_text)
    if not haystack:
        return False
    if excerpt in haystack:
        return True
    overlap = overlap_ratio(tokens(truncate(excerpt, 80)), tokens(haystack))
    return overlap >= 0.7


def summarize_signal(signal: Signal) -> str:
    return truncate(signal.excerpt or signal.title, 120)


__all__ = [
    "Classifier",
    "EvidenceAssessment",
    "ClaimImpact",
    "dedupe_key",
    "is_same_story",
    "excerpt_matches_source",
    "CATEGORY_WEIGHT",
    "AUTHORITY_STRENGTH",
]
