"""采集层：把来源变成统一的原始信号。

两种模式：

* ``offline``：读取随仓库分发的 fixture，任何机器上都能复现同一份行为；
* ``live``：按来源 URL 拉取 RSS/Atom 或页面，失败只记录覆盖缺口，不阻断整次运行。

线上模式刻意保持“最小可用”：不处理登录、反爬与付费墙，
把可说明的覆盖范围写进报告，而不是假装覆盖全网。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from .models import Signal, parse_dt
from .profile import Profile, SourceDef
from .text import clean_text, parse_iso, stable_id, truncate
from . import __version__

USER_AGENT = f"frontier-radar/{__version__} (+https://github.com/bayonetta-ccc/frontier-radar)"
HTTP_TIMEOUT = 12


@dataclass
class SourceAccess:
    source_id: str
    name: str
    kind: str
    authority: str
    status: str  # ok / empty / unreachable / disabled / not_configured
    fetched: int = 0
    used: int = 0
    note: str = ""
    url: str = ""


@dataclass
class IngestResult:
    signals: list[Signal]
    access: list[SourceAccess]
    mode: str
    synthetic: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok_sources(self) -> int:
        return sum(1 for a in self.access if a.status == "ok")

    @property
    def coverage_ratio(self) -> float:
        usable = [a for a in self.access if a.status != "disabled"]
        if not usable:
            return 0.0
        return round(self.ok_sources / len(usable), 3)


class OfflineCollector:
    """从 fixture 文件读取信号；同一文件可被多个来源复用。"""

    def __init__(self, profile: Profile, fixtures_dir: str | Path = "config/fixtures") -> None:
        self.profile = profile
        self.fixtures_dir = Path(fixtures_dir)

    def fetch(self, sources: Iterable[SourceDef]) -> IngestResult:
        sources = list(sources)
        payloads = self._load_all_payloads(sources)
        access: list[SourceAccess] = []
        all_signals: list[dict] = []
        for source in sources:
            matched: list[dict] = []
            for data in payloads:
                matched.extend(
                    s for s in data.get("signals", []) if str(s.get("source_id", "")) == source.id
                )
            all_signals.extend(matched)
            status = "ok" if matched else "empty"
            access.append(
                SourceAccess(
                    source.id, source.name, source.kind, source.authority,
                    status, len(matched), 0,
                    "演示数据（合成）" if any(data.get("demo") for data in payloads) else "",
                    source.url,
                )
            )
        synthetic = any(data.get("demo") for data in payloads)
        return IngestResult(
            signals=self._payload_to_signals(all_signals), access=access, mode="offline", synthetic=synthetic
        )

    def _load_all_payloads(self, sources: list[SourceDef]) -> list[dict]:
        """读取领域 fixture 目录下的全部信号文件，再按时间窗口过滤。

        这样 `--now` 就能重放历史：同一份 fixture 可以反复产生同样的运行结果。
        """
        directory = self.fixtures_dir / self.profile.slug / "signals"
        files: list[Path] = []
        if directory.is_dir():
            files.extend(sorted(directory.glob("*.json")))
        for source in sources:
            target = self._resolve(source.offline_file)
            if target and target not in files:
                files.append(target)
        payloads: list[dict] = []
        for path in files:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            data = loaded if isinstance(loaded, dict) else {"signals": loaded}
            data["_path"] = str(path)
            payloads.append(data)
        return payloads

    def _resolve(self, offline_file: str) -> Path | None:
        if not offline_file:
            return None
        candidates = [
            self.fixtures_dir / self.profile.slug / offline_file,
            self.fixtures_dir / offline_file,
            Path(offline_file),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _payload_to_signals(self, payload: list[dict]) -> list[Signal]:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        result: list[Signal] = []
        seen: set[str] = set()
        for item in payload:
            signal_id = str(item.get("signal_id") or f"sig_{len(result):03d}")
            if signal_id in seen:
                continue
            seen.add(signal_id)
            result.append(
                Signal(
                    signal_id=signal_id,
                    source_id=str(item.get("source_id", "")),
                    title=clean_text(str(item.get("title", ""))),
                    url=str(item.get("url", "")),
                    published_at=parse_dt(str(item.get("published_at", ""))),
                    retrieved_at=str(item.get("retrieved_at") or now),
                    excerpt=clean_text(str(item.get("excerpt", ""))),
                    author=str(item.get("author", "")),
                    lang=str(item.get("lang", "zh")),
                    raw={"fixture": True},
                )
            )
        return result


class LiveCollector:
    """按来源 URL 拉取 RSS/Atom 或 HTML 页面（尽力而为）。

    对可说明范围负责：抓取失败的来源会被标记为 unreachable，
    并在报告里体现为覆盖缺口，而不是静默消失。

    所有来源并发抓取，并有总时限；单个来源超时不会拖住整次运行。
    """

    def __init__(
        self,
        profile: Profile,
        lookback_days: int = 14,
        timeout: int = HTTP_TIMEOUT,
        total_timeout: int = 45,
        workers: int = 6,
    ) -> None:
        self.profile = profile
        self.lookback_days = lookback_days
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.workers = workers

    def fetch(self, sources: Iterable[SourceDef]) -> IngestResult:
        sources = list(sources)
        access: list[SourceAccess] = []
        signals: list[Signal] = []
        errors: list[str] = []
        targets = [s for s in sources if s.url]
        for source in sources:
            if not source.url:
                access.append(
                    SourceAccess(
                        source.id, source.name, source.kind, source.authority,
                        "not_configured", 0, 0, "没有配置抓取 URL", "",
                    )
                )
        if not targets:
            return IngestResult(signals=[], access=access, mode="live", synthetic=False)

        from concurrent.futures import ThreadPoolExecutor, wait

        fetched: dict[str, tuple[str, list[dict]]] = {}
        failed: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._fetch_one, source): source for source in targets}
            done, pending = wait(futures, timeout=self.total_timeout)
            for future, source in futures.items():
                if future in done:
                    try:
                        fetched[source.id] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        failed[source.id] = f"{type(exc).__name__}: {exc}"
                else:
                    failed[source.id] = "抓取超时（超过总时限）"
                    future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

        cutoff = datetime.now().astimezone() - timedelta(days=self.lookback_days)
        for source in targets:
            if source.id in failed:
                message = f"{source.name}: {failed[source.id]}"
                errors.append(message)
                access.append(
                    SourceAccess(
                        source.id, source.name, source.kind, source.authority,
                        "unreachable", 0, 0, truncate(failed[source.id], 160), source.url,
                    )
                )
                continue
            _url, items = fetched.get(source.id, ("", []))
            fresh = [
                i for i in items
                if parse_iso(i["published_at"]) >= cutoff
            ]
            for index, item in enumerate(fresh):
                signals.append(
                    Signal(
                        signal_id=stable_id("live", source.id, item["url"] or item["title"], str(index)),
                        source_id=source.id,
                        title=item["title"],
                        url=item["url"],
                        published_at=item["published_at"],
                        retrieved_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                        excerpt=item.get("excerpt", ""),
                        author=item.get("author", ""),
                        lang=item.get("lang", "zh"),
                        raw={"live": True},
                    )
                )
            access.append(
                SourceAccess(
                    source.id, source.name, source.kind, source.authority,
                    "ok" if fresh else "empty", len(items), len(fresh), "", source.url,
                )
            )
        return IngestResult(signals=signals, access=access, mode="live", synthetic=False, errors=errors)

    def _fetch_one(self, source: SourceDef) -> tuple[str, list[dict]]:
        body = self._http_get(source.url)
        items = self.parse_feed(body)
        if not items:
            items = self.parse_html(body, source)
        return source.url, items

    def _http_get(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, text/html"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - 用户配置的来源
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")

    @staticmethod
    def parse_feed(body: str) -> list[dict]:
        text = body.lstrip("\ufeff \n\r\t")
        if not text.startswith("<"):
            return []
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []
        items: list[dict] = []
        for node in root.iter():
            tag = node.tag.split("}")[-1].lower()
            if tag not in {"item", "entry"}:
                continue
            fields = {child.tag.split("}")[-1].lower(): (child.text or "").strip() for child in node}
            link = fields.get("link", "")
            for child in node:
                if child.tag.split("}")[-1].lower() == "link":
                    link = child.attrib.get("href", link)
            title = unescape(fields.get("title", ""))
            summary = unescape(fields.get("description") or fields.get("summary") or fields.get("content") or "")
            published = (
                fields.get("pubdate")
                or fields.get("published")
                or fields.get("updated")
                or fields.get("date")
                or ""
            )
            if title:
                items.append(
                    {
                        "title": clean_text(title),
                        "url": link,
                        "excerpt": truncate(_strip_tags(summary), 600),
                        "published_at": parse_dt(published),
                        "author": fields.get("author", ""),
                        "lang": "en",
                    }
                )
        return items[:60]

    @staticmethod
    def parse_html(body: str, source: SourceDef) -> list[dict]:
        items: list[dict] = []
        for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, re.S | re.I):
            href, label = match.group(1), _strip_tags(match.group(2))
            if len(label) < 20 or not href.startswith("http"):
                continue
            items.append(
                {
                    "title": clean_text(label),
                    "url": href,
                    "excerpt": "",
                    "published_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "author": source.name,
                    "lang": "zh",
                }
            )
            if len(items) >= 30:
                break
        return items


def _strip_tags(value: str) -> str:
    return clean_text(unescape(re.sub(r"<[^>]+>", " ", value or "")))


def collector_for(profile: Profile, mode: str, fixtures_dir: str | Path = "config/fixtures"):
    if mode == "live":
        return LiveCollector(profile)
    return OfflineCollector(profile, fixtures_dir)


__all__ = ["SourceAccess", "IngestResult", "OfflineCollector", "LiveCollector", "collector_for"]
