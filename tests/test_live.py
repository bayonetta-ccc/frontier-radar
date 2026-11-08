"""在线采集测试：用本地 HTTP 服务模拟 RSS，验证超时、失败与解析，不访问外网。"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from frontier_radar.profile import SourceDef, load_profile
from frontier_radar.sources import LiveCollector

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Demo feed</title>
  <item>
    <title>Toyota reaffirms solid-state battery timeline</title>
    <link>https://example.com/toyota/feed-item-1</link>
    <description>Toyota said the all-solid-state battery remains on track for 2027-2028 mass production.</description>
    <pubDate>{date}</pubDate>
  </item>
</channel></rss>
"""


class FeedHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/fail":
            self.send_response(500)
            self.end_headers()
            return
        body = RSS.format(date=_rfc822(datetime.now(timezone.utc))).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 安静
        return


def _rfc822(value: datetime) -> str:
    return value.strftime("%a, %d %b %Y %H:%M:%S GMT")


class LiveCollectorCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.profile = load_profile("solid_state_battery")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def source(self, path: str, source_id: str = "toyota_newsroom") -> SourceDef:
        return SourceDef(
            id=source_id,
            name="Local feed",
            kind="official",
            authority="high",
            url=f"http://127.0.0.1:{self.port}{path}",
            independent_group="local",
        )

    def test_fetch_parses_local_feed(self) -> None:
        collector = LiveCollector(self.profile, timeout=5, total_timeout=10)
        result = collector.fetch([self.source("/feed")])
        self.assertEqual(result.mode, "live")
        self.assertFalse(result.synthetic)
        self.assertEqual(len(result.signals), 1)
        signal = result.signals[0]
        self.assertIn("solid-state", signal.title.lower())
        self.assertEqual(signal.url, "https://example.com/toyota/feed-item-1")
        self.assertEqual(result.access[0].status, "ok")
        self.assertGreaterEqual(result.coverage_ratio, 0.99)

    def test_fetch_marks_unreachable(self) -> None:
        collector = LiveCollector(self.profile, timeout=5, total_timeout=10)
        result = collector.fetch([self.source("/fail")])
        self.assertEqual(result.signals, [])
        self.assertEqual(result.access[0].status, "unreachable")
        self.assertTrue(result.errors)

    def test_non_configured_source(self) -> None:
        collector = LiveCollector(self.profile, timeout=5, total_timeout=10)
        source = SourceDef(id="x", name="No url", kind="media", authority="medium")
        result = collector.fetch([source])
        self.assertEqual(result.access[0].status, "not_configured")

    def test_offline_collector_filters_by_profile(self) -> None:
        from frontier_radar.sources import OfflineCollector

        collector = OfflineCollector(self.profile, "config/fixtures")
        result = collector.fetch(self.profile.sources)
        self.assertGreaterEqual(len(result.signals), 15)
        self.assertTrue(result.synthetic)
        self.assertEqual(
            {s.source_id for s in result.signals},
            {a.source_id for a in result.access if a.status == "ok"},
        )


if __name__ == "__main__":
    unittest.main()
