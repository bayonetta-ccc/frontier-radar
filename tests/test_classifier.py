"""单元测试：文本工具、分类器、证据折算与配置校验。"""

from __future__ import annotations

import unittest

from frontier_radar.claims import Thresholds
from frontier_radar.evidence import Classifier, _accumulate, is_same_story
from frontier_radar.models import InfoLayer, Signal
from frontier_radar.profile import load_profile
from frontier_radar.sources import LiveCollector
from frontier_radar.text import (
    canonical_url,
    clean_text,
    content_hash,
    jaccard,
    parse_iso,
    tokens,
    url_key,
)
from frontier_radar.validate import validate_profile


def make_signal(title: str, excerpt: str = "", source_id: str = "toyota_newsroom") -> Signal:
    return Signal(
        signal_id="sig_test",
        source_id=source_id,
        title=title,
        url="https://example.com/a?utm_source=x",
        published_at="2026-08-01T10:00:00+08:00",
        retrieved_at="2026-08-02T10:00:00+08:00",
        excerpt=excerpt or title,
    )


class TextCase(unittest.TestCase):
    def test_canonical_url_strips_tracking(self) -> None:
        self.assertEqual(
            canonical_url("https://www.Example.com/a/?utm_source=x&id=3#frag"),
            "https://example.com/a?id=3",
        )

    def test_url_key_stable(self) -> None:
        self.assertEqual(
            url_key("https://example.com/a?utm_medium=x"),
            url_key("https://www.example.com/a"),
        )

    def test_tokens_cjk_and_english(self) -> None:
        result = tokens("硫化物 solid-state 2027")
        self.assertIn("硫化", result)
        self.assertIn("solid-state", result)
        self.assertIn("2027", result)

    def test_jaccard_identical(self) -> None:
        self.assertEqual(jaccard(tokens("固态电池量产"), tokens("固态电池量产")), 1.0)

    def test_parse_iso_timezone(self) -> None:
        self.assertEqual(parse_iso("2026-08-01T10:00:00+08:00").hour, 2)

    def test_parse_iso_date_only(self) -> None:
        self.assertEqual(parse_iso("2026-08-01").day, 1)

    def test_content_hash_ignores_spacing(self) -> None:
        self.assertEqual(
            content_hash("固态 电池", "量产 计划"),
            content_hash("固态电池", "量产计划"),
        )

    def test_clean_text_nfkc(self) -> None:
        self.assertEqual(clean_text("ＡＢＣ　固态电池"), "ABC 固态电池")


class ClassifierCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = load_profile("solid_state_battery")
        cls.classifier = Classifier(cls.profile)

    def test_standard_category_not_overridden_by_mass_production(self) -> None:
        signal = make_signal(
            "固态电池标准草案进入公开征求意见阶段",
            "标准化技术委员会公告，公开征求意见，尚未正式发布，涉及测试方法与安全规范。",
            source_id="miit_standards",
        )
        judgement = self.classifier.classify(signal)
        self.assertEqual(judgement.category, "standard")
        self.assertIn(judgement.info_layer, {InfoLayer.FACT, InfoLayer.COMPANY_CLAIM})
        self.assertEqual(judgement.stage, 4)

    def test_pilot_line_stage(self) -> None:
        signal = make_signal(
            "宁德时代硫化物固态电池中试线进入设备安装",
            "中试线进入设备安装阶段，规划小批量交付，用于验证电解质工艺与良率。",
            source_id="catl_news",
        )
        judgement = self.classifier.classify(signal)
        self.assertEqual(judgement.category, "pilot_line")
        self.assertEqual(judgement.stage, 2)
        self.assertIn("catl", judgement.entities)
        self.assertIn("route_competition", judgement.anchors)

    def test_delay_polarity_negative(self) -> None:
        signal = make_signal(
            "日产将固态电池中试线投产时间推迟一年，称良率未达预期",
            "由于良率未达预期，原定中试线投产将推迟一年，公司未给出新的量产时间表。",
            source_id="automotive_media",
        )
        judgement = self.classifier.classify(signal)
        self.assertEqual(judgement.polarity, -1)

    def test_denial_with_not_completed_is_negative_not_positive(self) -> None:
        signal = make_signal(
            "宝马澄清：QuantumScape 尚未完成整车级验证，仅处于样件测试阶段",
            "宝马发言人澄清，样件仍处于台架与样件测试阶段，尚未完成整车级验证，也未进入量产定点。",
            source_id="automotive_media",
        )
        judgement = self.classifier.classify(signal)
        self.assertEqual(judgement.polarity, -1)
        self.assertIn("quantumscape_validation", judgement.anchors)
        self.assertLessEqual(judgement.stage, 2)

    def test_rumor_is_noise_for_low_authority(self) -> None:
        signal = make_signal(
            "论坛热帖：听说宁德时代明年就要量产固态电池了",
            "有网友称听说明年就能买到搭载宁德时代固态电池的车，但未提供任何来源或证据。",
            source_id="social_forum",
        )
        judgement = self.classifier.classify(signal)
        self.assertTrue(judgement.noise_reason)
        self.assertEqual(judgement.info_layer, InfoLayer.RUMOR)

    def test_marketing_noise(self) -> None:
        signal = make_signal(
            "颠覆性突破！固态电池将重新定义电动车，全球首个量产时代来临",
            "文章以颠覆性突破为标题讨论前景，正文主要是营销表述与概念介绍，没有可核验证据。",
            source_id="battery_industry_media",
        )
        judgement = self.classifier.classify(signal)
        self.assertTrue(judgement.noise_reason)

    def test_off_topic_noise(self) -> None:
        signal = make_signal(
            "碳酸锂价格反弹，固态电池概念股集体上涨",
            "受碳酸锂价格反弹影响，固态电池概念股集体上涨，市场情绪升温但产业基本面没有新变化。",
            source_id="automotive_media",
        )
        judgement = self.classifier.classify(signal)
        self.assertIn("无关", judgement.noise_reason)

    def test_anchor_requires_matching_entity(self) -> None:
        signal = make_signal(
            "某车企宣布扩建电池工厂",
            "该车企宣布扩建电池工厂，新增产能用于现有液态锂电池产品。",
            source_id="automotive_media",
        )
        judgement = self.classifier.classify(signal)
        self.assertEqual(judgement.anchors, ())

    def test_official_claim_layer(self) -> None:
        signal = make_signal(
            "丰田宣布固态电池试产线 2026 年启动",
            "丰田宣布试产线将于 2026 年内启动，并再次确认 2027—2028 年量产装车计划不变。",
        )
        judgement = self.classifier.classify(signal)
        self.assertEqual(judgement.info_layer, InfoLayer.COMPANY_CLAIM)
        self.assertIn("toyota_timeline", judgement.anchors)

    def test_paper_layer_fact_but_stage_zero(self) -> None:
        signal = make_signal(
            "硫化物固态电解质公斤级制备研究",
            "研究团队报告公斤级制备路线，实验室循环 200 次后容量保持率 82%，论文为预印本。",
            source_id="arxiv_battery",
        )
        judgement = self.classifier.classify(signal)
        self.assertEqual(judgement.category, "research")
        self.assertEqual(judgement.stage, 0)
        self.assertEqual(judgement.info_layer, InfoLayer.FACT)

    def test_timestamp_filter_is_not_classifier_business(self) -> None:
        # 分类器不应吞掉“旧闻”：时间窗口由 pipeline 负责
        old = make_signal("丰田重申固态电池量产计划", "丰田再次确认 2027—2028 年量产装车目标不变。")
        old.published_at = "2026-07-02T10:00:00+09:00"
        judgement = self.classifier.classify(old)
        self.assertIn("toyota_timeline", judgement.anchors)


class EvidenceWeightCase(unittest.TestCase):
    def test_accumulate_diminishing(self) -> None:
        from frontier_radar.evidence import EvidenceAssessment

        one = _accumulate([EvidenceAssessment("a", 1, 0.5)])
        two = _accumulate([EvidenceAssessment("a", 1, 0.5), EvidenceAssessment("b", 1, 0.5)])
        self.assertGreater(two, one)
        self.assertLess(two, 1.0)


class DedupeCase(unittest.TestCase):
    def test_same_url(self) -> None:
        self.assertTrue(
            is_same_story(
                {"url_key": "abc", "title": "A"},
                {"url_key": "abc", "title": "B"},
            )
        )

    def test_similar_title(self) -> None:
        self.assertTrue(
            is_same_story(
                {"url_key": "x", "title": "丰田重申全固态电池 2027—2028 年量产目标"},
                {"url_key": "y", "title": "丰田重申全固态电池 2027—2028 年量产目标（转载）"},
                threshold=0.8,
            )
        )

    def test_different_story(self) -> None:
        self.assertFalse(
            is_same_story(
                {"url_key": "x", "title": "宁德时代中试线进入设备安装"},
                {"url_key": "y", "title": "QuantumScape 交付 B 样电芯"},
            )
        )


class FeedParseCase(unittest.TestCase):
    def test_parse_rss(self) -> None:
        body = """<?xml version="1.0"?><rss><channel><item>
        <title>Solid state battery pilot line starts</title>
        <link>https://example.com/a</link>
        <description>Pilot line starts in 2026.</description>
        <pubDate>Fri, 01 Aug 2026 02:00:00 GMT</pubDate>
        </item></channel></rss>"""
        items = LiveCollector.parse_feed(body)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://example.com/a")
        self.assertIn("2026", items[0]["published_at"])

    def test_parse_atom(self) -> None:
        body = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>Battery update</title><link href="https://example.com/b"/>
        <updated>2026-08-01T00:00:00Z</updated><summary>Something new.</summary>
        </entry></feed>"""
        items = LiveCollector.parse_feed(body)
        self.assertEqual(items[0]["url"], "https://example.com/b")

    def test_parse_garbage(self) -> None:
        self.assertEqual(LiveCollector.parse_feed("<html>not xml"), [])


class ProfileValidationCase(unittest.TestCase):
    def test_demo_profile_is_valid(self) -> None:
        report = validate_profile(load_profile("solid_state_battery"))
        self.assertEqual(report["errors"], [])
        self.assertGreaterEqual(len(report["lines"]), 2)

    def test_thresholds_defaults_are_conservative(self) -> None:
        thresholds = Thresholds()
        self.assertGreater(thresholds.strengthen_min, 0.1)
        self.assertGreater(thresholds.min_independent_groups, 1)
        self.assertTrue(thresholds.high_risk_review)


if __name__ == "__main__":
    unittest.main()
