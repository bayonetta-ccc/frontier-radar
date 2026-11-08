"""领域配置校验：把“配置错误”挡在运行之前。"""

from __future__ import annotations

from typing import Any

from .models import InfoLayer
from .profile import Profile


def validate_profile(profile: Profile) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    lines: list[str] = [f"领域：{profile.name}（{profile.slug}）"]

    if not profile.slug:
        errors.append("slug 不能为空")
    if not profile.stages:
        errors.append("stages 不能为空")
    levels = [s.level for s in profile.stages]
    if levels != list(range(len(levels))):
        warnings.append(f"阶段刻度不连续：{levels}（建议从 0 连续编号）")
    for stage in profile.stages:
        if not stage.keywords and stage.key not in (profile.stage_patterns or {}):
            warnings.append(f"阶段 {stage.key} 既无 keywords 也无 stage_patterns，将永远不会被识别")

    if not profile.entities:
        warnings.append("entities 为空：只能依靠关键词归因，噪声会变多")
    aliases: dict[str, list[str]] = {}
    for entity in profile.entities:
        for token in entity.tokens():
            aliases.setdefault(token.lower(), []).append(entity.key)
    for token, owners in aliases.items():
        if len(set(owners)) > 1:
            warnings.append(f"别名 “{token}” 同时指向 {sorted(set(owners))}，可能造成错误归因")

    if not profile.anchors:
        errors.append("anchors 不能为空：没有判断锚点就无法建立判断状态库")
    anchor_keys = {a.key for a in profile.anchors}
    for anchor in profile.anchors:
        if not anchor.key or not anchor.name:
            errors.append(f"anchor 缺少 key/name：{anchor}")
        if anchor.entity_keys:
            unknown = [k for k in anchor.entity_keys if k not in {e.key for e in profile.entities}]
            if unknown:
                errors.append(f"anchor {anchor.key} 引用了不存在的 entity：{unknown}")
        elif not anchor.claim_template:
            warnings.append(f"anchor {anchor.key} 既无 entity 又无 claim_template，阶段变化时难以改写表述")

    entity_keys = {e.key for e in profile.entities}
    source_groups: dict[str, list[str]] = {}
    for source in profile.sources:
        if not source.id:
            errors.append("source 缺少 id")
        if source.authority not in {"high", "medium", "low"}:
            errors.append(f"source {source.id} 的 authority 非法：{source.authority}")
        if not source.url and not source.offline_file:
            warnings.append(f"source {source.id} 既无 url 也无 offline_file，只会被记为覆盖缺口")
        source_groups.setdefault(source.independent_group, []).append(source.id)
    if len(source_groups) < 2:
        warnings.append("独立来源组少于 2 个：无法做独立印证判断")

    if not profile.category_blueprint:
        warnings.append("缺少 category_blueprint：类别判断将回退到简单词频，准确率会下降")
    else:
        for category, entries in profile.category_blueprint.items():
            for entry in entries:
                if not entry.get("pattern"):
                    errors.append(f"category_blueprint.{category} 存在空 pattern")
                try:
                    float(entry.get("score", 0))
                except (TypeError, ValueError):
                    errors.append(f"category_blueprint.{category} 的 score 不是数字")

    if not profile.info_layers:
        warnings.append("缺少 info_layers：事实/口径/推断/观点/传闻无法分层")
    else:
        for layer in profile.info_layers:
            if layer not in {item.value for item in InfoLayer}:
                warnings.append(f"info_layers 中的 “{layer}” 不是已知信息层，将被忽略")

    if not profile.coverage_notes:
        warnings.append("缺少 coverage_notes：报告将无法说明覆盖缺口")

    lines.append(f"阶段 {len(profile.stages)} · 主体 {len(profile.entities)} · 判断锚点 {len(profile.anchors)} · 来源 {len(profile.sources)}")
    if profile.category_blueprint:
        lines.append(f"类别蓝图 {len(profile.category_blueprint)} 类 · 阶段模式 {len(profile.stage_patterns)} 组")
    lines.append(f"独立来源组 {len(source_groups)}：{'、'.join(sorted(source_groups))}")
    for warning in warnings:
        lines.append(f"[!] 警告：{warning}")
    for error in errors:
        lines.append(f"[ERR] 错误：{error}")
    return {"errors": errors, "warnings": warnings, "lines": lines}


def validate_fixtures(profile: Profile, fixtures_dir: str) -> dict[str, Any]:
    """检查 fixture 中的来源、时间与字段完整性。"""
    import json
    from pathlib import Path

    from .text import parse_iso

    directory = Path(fixtures_dir) / profile.slug / "signals"
    errors: list[str] = []
    warnings: list[str] = []
    known_sources = {s.id for s in profile.sources}
    count = 0
    files = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not files:
        warnings.append(f"fixture 目录为空：{directory}")
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for item in data.get("signals", []):
            count += 1
            if item.get("source_id") not in known_sources:
                errors.append(f"{path.name}: 未知 source_id {item.get('source_id')}")
            if not item.get("url", "").startswith("http"):
                warnings.append(f"{path.name}: 信号缺少 http 链接 {item.get('signal_id')}")
            if len(str(item.get("excerpt", ""))) < 24:
                warnings.append(f"{path.name}: 摘录过短，会被判为噪声 {item.get('signal_id')}")
            try:
                parse_iso(str(item.get("published_at", "")))
            except ValueError:
                errors.append(f"{path.name}: 时间非法 {item.get('published_at')}")
    return {"errors": errors, "warnings": warnings, "signals": count, "files": len(files)}


__all__ = ["validate_profile", "validate_fixtures"]
