"""文本、链接、时间等小工具。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CJK = r"\u4e00-\u9fff\u3040-\u30ff"
_CJK_RE = re.compile(f"[{CJK}]")
_TOKEN_RE = re.compile(f"[{CJK}]+|[a-zA-Z][a-zA-Z0-9.+-]*|\\d{{4}}")
_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff\u3040-\u30ff+#.-]+", re.UNICODE)

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by",
    "at", "from", "as", "is", "are", "was", "were", "be", "been", "this", "that",
    "it", "its", "their", "will", "has", "have", "had", "not", "no", "new",
}
CN_STOP_BIGRAMS = {
    "我们", "他们", "这个", "那个", "一个", "以及", "并且", "但是", "因为", "所以",
    "可以", "已经", "正在", "进行", "目前", "表示", "认为", "对于", "关于", "通过",
    "相关", "方面", "问题", "情况", "同时", "此外", "然而", "如果", "由于",
}

TRACKING_QUERY_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "spm", "from", "share", "ref", "referrer", "fbclid", "gclid", "mc_cid", "mc_eid",
}


def clean_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = text.replace("\u3000", " ")
    return _SPACE_RE.sub(" ", text).strip()


def canonical_url(url: str) -> str:
    """去掉追踪参数与锚点，统一大小写与尾斜杠，用于同文去重。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
    path = parts.path.rstrip("/") or "/"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_QUERY_PARAMS and not k.lower().startswith("utm_")
    ]
    return urlunsplit((scheme, host, path, urlencode(sorted(query)), ""))


def url_key(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def content_hash(title: str, excerpt: str) -> str:
    """对标题+正文做“空白无关”的哈希，用于识别同稿多投。"""
    title_norm = re.sub(r"\s+", "", clean_text(title).lower())
    excerpt_norm = re.sub(r"\s+", "", clean_text(excerpt)[:400].lower())
    payload = f"{title_norm}|{excerpt_norm}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def stable_id(prefix: str, *parts: str) -> str:
    payload = "|".join(clean_text(p).lower() for p in parts)
    return f"{prefix}_{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:10]}"


def tokens(text: str) -> set[str]:
    """中英混合分词：中文按字 + 二元组，英文按词。用于粗糙但稳定的相似度。"""
    text = clean_text(text).lower()
    result: set[str] = set()
    for chunk in _TOKEN_RE.findall(text):
        if _CJK_RE.fullmatch(chunk[0]):
            result.update(ch for ch in chunk if ch.strip())
            result.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
        elif chunk[0].isdigit():
            result.add(chunk)
        elif chunk not in STOPWORDS and len(chunk) > 1:
            result.add(chunk)
    return {t for t in result if t not in CN_STOP_BIGRAMS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap_ratio(small: set[str], large: set[str]) -> float:
    if not small:
        return 0.0
    return len(small & large) / len(small)


def truncate(text: str, limit: int = 180) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    text = (value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # RSS 常见的 RFC 2822 时间：Fri, 01 Aug 2026 02:00:00 GMT
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            dt = None
        if dt is None:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_date(value: str) -> str:
    return parse_iso(value).date().isoformat()


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def confidence_label(value: float) -> str:
    if value >= 0.75:
        return "较高"
    if value >= 0.55:
        return "中等"
    if value >= 0.35:
        return "偏低"
    return "低"


def reliability_label(value: float) -> str:
    if value >= 0.8:
        return "高"
    if value >= 0.55:
        return "中"
    return "低"


def load_json(path: str | "object") -> object:  # pragma: no cover - 便捷读取
    from pathlib import Path

    return json.loads(Path(path).read_text(encoding="utf-8"))


def dumps_pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def domain_of(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host
