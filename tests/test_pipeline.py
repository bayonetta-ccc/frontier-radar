"""端到端测试：演示链、判断更新、去重、审批、回滚与指标。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from frontier_radar.claims import Thresholds
from frontier_radar.db import Database
from frontier_radar.fixtures import load_demo_task, run_demo
from frontier_radar.models import ChangeKind, ClaimStatus
from frontier_radar.pipeline import RadarPipeline
from frontier_radar.profile import load_profile


def _prepare_fixtures(destination: Path, extra_signals: list[dict]) -> None:
    """把演示 fixture 复制到临时目录，并追加一条“下一期”信号。"""
    import shutil

    source = Path("config/fixtures/solid_state_battery/signals")
    target = destination / "solid_state_battery" / "signals"
    target.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.json"):
        if path.name in {"baseline.json", "week-2.json"}:
            shutil.copy(path, target / path.name)
    baseline = json.loads((target / "baseline.json").read_text(encoding="utf-8"))
    baseline["signals"].extend(extra_signals)
    (target / "baseline.json").write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _bmw_clarification_signal() -> dict:
    return {
        "signal_id": "sig_test_bmw",
        "source_id": "automotive_media",
        "title": "宝马发言人：QuantumScape 样件尚未完成整车级验证",
        "url": "https://example.com/auto-media/bmw-test",
        "published_at": "2026-08-07T08:30:00+08:00",
        "excerpt": "宝马发言人表示，QuantumScape 的样件仍处于台架测试，尚未完成整车级验证，也未进入量产定点。",
        "author": "汽车产业观察",
        "lang": "zh",
    }


def _toyota_followup_signal() -> dict:
    return {
        "signal_id": "sig_test_toyota_followup",
        "source_id": "toyota_newsroom",
        "title": "丰田补充说明固态电池试产线与量产节点安排",
        "url": "https://example.com/toyota/newsroom/followup-test",
        "published_at": "2026-08-10T10:00:00+08:00",
        "excerpt": "丰田发布补充说明，试产线按计划推进，2027—2028 年量产装车目标保持不变，首批应用于高端车型。",
        "author": "Toyota Newsroom",
        "lang": "en",
    }


class DemoCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls.tmp.name) / "data"
        cls.output = run_demo(data_dir=cls.data_dir)
        cls.db = Database(cls.output.db_path)
        cls.task_id = cls.output.task_id

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()
        cls.tmp.cleanup()

    def test_baseline_creates_claims_pending_review(self) -> None:
        baseline = self.output.runs[0]
        self.assertEqual(baseline.digest.mode, "baseline")
        self.assertGreaterEqual(len(baseline.baseline_claims), 3)
        self.assertLessEqual(len(baseline.baseline_claims), 5)
        for claim in baseline.baseline_claims:
            self.assertTrue(claim.needs_review)
        self.assertIn("初始基线", baseline.digest.summary["headline"])

    def test_baseline_separates_duplicates_and_noise(self) -> None:
        baseline = self.output.runs[0].digest
        self.assertGreaterEqual(baseline.summary["duplicate_count"], 1)
        self.assertGreaterEqual(baseline.summary["noise_count"], 1)
        reasons = " ".join(item["reason"] for item in baseline.not_pushed)
        self.assertIn("重复", reasons)
        self.assertTrue(
            any("传闻" in item["reason"] or "无关" in item["reason"] for item in baseline.not_pushed)
        )

    def test_update_detects_falsification(self) -> None:
        """引擎级测试：通过真实 run 注入“当事方澄清”信号，应触发证伪并停在待确认。"""
        tmp = tempfile.TemporaryDirectory()
        db = None
        try:
            data_dir = Path(tmp.name) / "data"
            fixtures = Path(tmp.name) / "fixtures"
            _prepare_fixtures(fixtures, extra_signals=[_bmw_clarification_signal()])
            db = Database(data_dir / "radar.db")
            pipeline = RadarPipeline(db, load_profile("solid_state_battery"), fixtures_dir=fixtures, artifact_root=data_dir / "artifacts")
            task = load_demo_task()
            task["task_id"] = "task_falsify_case"
            pipeline.create_task(task)
            baseline = pipeline.run(
                task["task_id"], mode="baseline", now="2026-07-28T09:00:00+08:00", period_days=40
            )
            for claim in baseline.baseline_claims:
                pipeline.engine.confirm(claim.claim_id, now="2026-07-28T10:00:00+08:00")
            claim = next(
                c for c in db.list_claims(task["task_id"]) if c.anchor_key == "quantumscape_validation"
            )
            update = pipeline.run(
                task["task_id"], mode="update", now="2026-08-08T09:00:00+08:00", period_days=14
            )
            target = next(
                (c for c in update.changed if c.claim_id == claim.claim_id), None
            )
            self.assertIsNotNone(target)
            assert target is not None
            self.assertIs(target.kind, ChangeKind.FALSIFY)
            self.assertTrue(target.requires_confirmation)
            self.assertLess(target.confidence_after, target.confidence_before)
            self.assertTrue(target.contradicting)
            self.assertIn(target.contradicting[0].info_layer.value, {"fact", "company_claim"})
        finally:
            if db:
                db.close()
            tmp.cleanup()

    def test_update_strengthens_with_independent_evidence(self) -> None:
        update = self.output.runs[1].digest
        strengthened = [c for c in update.changes if c.kind is ChangeKind.STRENGTHEN]
        self.assertGreaterEqual(len(strengthened), 1)
        for change in strengthened:
            self.assertGreater(change.confidence_after, change.confidence_before)
            self.assertTrue(change.supporting)
            self.assertTrue(any(s.url for s in change.supporting), "变化卡必须能回到原始链接")

    def test_quiet_period_does_not_force_update(self) -> None:
        quiet = self.output.runs[2].digest
        self.assertEqual(quiet.changes, [])
        self.assertEqual(quiet.mode, "update")
        self.assertIn("无需更新", quiet.summary["headline"])
        self.assertTrue(quiet.not_pushed)

    def test_claims_state_and_versions(self) -> None:
        claims = self.db.list_claims(self.task_id)
        self.assertGreaterEqual(len(claims), 3)
        pending = [c for c in claims if c.pending_review]
        self.assertTrue(pending, "演示结束时应有待确认修改，等待人工核对")
        claim = pending[0]
        versions = self.db.list_claim_versions(claim.claim_id)
        self.assertGreaterEqual(len(versions), 2)
        self.assertEqual(versions[-1].change_kind, "baseline")
        self.assertEqual(versions[0].user_correction, "pending")
        self.assertTrue(all(v.reason for v in versions), "每个版本都必须留下理由")
        # 证伪判断的证据：当前依据应全部是相反证据
        falsified = next(c for c in claims if c.anchor_key == "quantumscape_validation")
        evidence = self.db.evidence_for_claim(falsified.claim_id)
        self.assertTrue(evidence)
        self.assertTrue(all(item["direction"] == "contra" for item in evidence))

    def test_pending_change_not_stacked(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        db = None
        try:
            data_dir = Path(tmp.name) / "data"
            fixtures = Path(tmp.name) / "fixtures"
            _prepare_fixtures(fixtures, extra_signals=[_toyota_followup_signal()])
            db = Database(data_dir / "radar.db")
            pipeline = RadarPipeline(
                db,
                load_profile("solid_state_battery"),
                fixtures_dir=fixtures,
                artifact_root=data_dir / "artifacts",
            )
            task = load_demo_task()
            task["task_id"] = "task_pending_case"
            pipeline.create_task(task)
            baseline = pipeline.run(
                task["task_id"], mode="baseline", now="2026-07-28T09:00:00+08:00", period_days=40
            )
            for claim in baseline.baseline_claims:
                pipeline.engine.confirm(claim.claim_id, now="2026-07-28T10:00:00+08:00")
            pipeline.run(task["task_id"], mode="update", now="2026-08-08T09:00:00+08:00", period_days=14)
            pending = {
                c.claim_id: c
                for c in db.list_claims(task["task_id"])
                if c.pending_review
            }
            self.assertTrue(pending, "演示应产生待确认修改")
            versions_before = {cid: db.latest_version_number(cid) for cid in pending}
            second = pipeline.run(
                task["task_id"], mode="update", now="2026-08-15T09:00:00+08:00", period_days=7
            )
            for claim_id, version in versions_before.items():
                self.assertEqual(db.latest_version_number(claim_id), version, "待确认修改不应被叠加")
            unresolved = [e for e in second.evaluations if e.status == "unresolved"]
            self.assertTrue(unresolved, "有新证据但存在待确认修改时，应显式标记未解决")
            self.assertTrue(any("等待用户确认" in e.reason for e in unresolved))
        finally:
            if db:
                db.close()
            tmp.cleanup()

    def test_reports_exported(self) -> None:
        for result in self.output.runs:
            base = self.data_dir / "artifacts" / self.task_id / result.run_id
            self.assertTrue((base / "digest.json").exists())
            self.assertTrue((base / "digest.md").exists())
            payload = json.loads((base / "digest.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], result.run_id)
            self.assertIn("signal_index", payload)

    def test_metrics_and_gates(self) -> None:
        from frontier_radar.engine import build_metrics

        metrics = build_metrics(self.db, self.task_id)
        self.assertEqual(metrics["task"]["runs"], 3)
        self.assertLessEqual(metrics["information_quality"]["duplicate_rate"], 0.2)
        self.assertGreaterEqual(metrics["information_quality"]["link_integrity"], 0.99)
        self.assertTrue(metrics["judgement_update_quality"]["versions_with_reason"] >= 0.99)
        self.assertTrue(metrics["quality_gates"]["pass"])


class ApprovalCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        run_demo(data_dir=self.data_dir, reset=True)
        self.db = Database(self.data_dir / "radar.db")
        self.task_id = "task_solid_state_2026"
        self.pipeline = RadarPipeline(
            self.db,
            load_profile("solid_state_battery"),
            fixtures_dir="config/fixtures",
            artifact_root=self.data_dir / "artifacts",
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_rollback_creates_new_version(self) -> None:
        for claim in self.db.list_claims(self.task_id):
            if claim.pending_review:
                self.pipeline.engine.confirm(claim.claim_id, note="测试确认", now="2026-08-16T09:00:00+08:00")
        claim = next(
            c for c in self.db.list_claims(self.task_id) if c.status is ClaimStatus.ACTIVE
        )
        latest = self.db.latest_version_number(claim.claim_id)
        updated = self.pipeline.engine.rollback(claim.claim_id, 1, note="测试回滚")
        self.assertEqual(updated.version, latest + 1)
        self.assertEqual(self.db.latest_version_number(claim.claim_id), latest + 1)
        self.assertEqual(updated.statement, self.db.get_claim_version(claim.claim_id, 1).statement)

    def test_confirm_clears_pending(self) -> None:
        task = load_demo_task()
        task["task_id"] = "task_confirm_case"
        self.pipeline.create_task(task)
        baseline = self.pipeline.run(task["task_id"], mode="baseline", now="2026-07-28T09:00:00+08:00", period_days=40)
        self.assertTrue(baseline.baseline_claims)
        claim = baseline.baseline_claims[0]
        confirmed = self.pipeline.engine.confirm(claim.claim_id, note="ok", now="2026-07-29T09:00:00+08:00")
        self.assertFalse(confirmed.pending_review)
        self.assertEqual(confirmed.status, ClaimStatus.ACTIVE)
        with self.assertRaises(ValueError):
            self.pipeline.engine.confirm(claim.claim_id)

    def test_reject_keeps_old_statement(self) -> None:
        task = load_demo_task()
        task["task_id"] = "task_reject_case"
        self.pipeline.create_task(task)
        self.pipeline.run(task["task_id"], mode="baseline", now="2026-07-28T09:00:00+08:00", period_days=40)
        update = self.pipeline.run(task["task_id"], mode="update", now="2026-08-08T09:00:00+08:00", period_days=14)
        self.assertTrue(update.changed)
        before = {c.claim_id: c.statement for c in self.db.list_claims(task["task_id"])}
        rejected = None
        for claim in self.db.list_claims(task["task_id"]):
            if claim.pending_review:
                rejected = self.pipeline.engine.reject(claim.claim_id, note="证据不足")
                break
        self.assertIsNotNone(rejected)
        assert rejected is not None
        self.assertEqual(rejected.statement, before[rejected.claim_id])
        self.assertFalse(rejected.pending_review)


class HumanoidScenarioCase(unittest.TestCase):
    """第二个演示场景：客户案例 → 被澄清；发布会必须被拦在判断之外。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls.tmp.name) / "data"
        cls.output = run_demo(
            data_dir=cls.data_dir, profile="humanoid_robot_supply_chain"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_baseline_has_three_claims(self) -> None:
        self.assertEqual(len(self.output.runs[0].baseline_claims), 3)

    def test_launch_and_report_are_suppressed(self) -> None:
        week2 = self.output.runs[1].digest
        reasons = " ".join(item["reason"] for item in week2.not_pushed)
        self.assertIn("发布会", reasons)
        self.assertIn("观点", reasons)
        self.assertTrue(
            any("产能 10 万台" in item["title"] for item in week2.not_pushed),
            "发布会口径不应进入判断",
        )

    def test_customer_case_strengthens_then_falsified(self) -> None:
        week2 = self.output.runs[1].digest
        self.assertTrue(any(c.kind is ChangeKind.STRENGTHEN for c in week2.changes))
        correction = self.output.runs[2].digest
        kinds = {c.kind for c in correction.changes}
        self.assertIn(ChangeKind.FALSIFY, kinds)
        self.assertIn(ChangeKind.WEAKEN, kinds)
        self.assertIn(ChangeKind.SHIFT, kinds)

    def test_quiet_period(self) -> None:
        quiet = self.output.runs[3].digest
        self.assertEqual(quiet.changes, [])
        self.assertIn("无需更新", quiet.summary["headline"])


class IsolationCase(unittest.TestCase):
    def test_engine_rejects_backwards_time(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        db = None
        try:
            data_dir = Path(tmp.name) / "data"
            db = Database(data_dir / "radar.db")
            pipeline = RadarPipeline(
                db, load_profile("solid_state_battery"), artifact_root=data_dir / "artifacts"
            )
            task = load_demo_task()
            task["task_id"] = "task_time_case"
            pipeline.create_task(task)
            pipeline.run(task["task_id"], mode="baseline", now="2026-07-28T09:00:00+08:00")
            with self.assertRaises(ValueError):
                pipeline.run(task["task_id"], now="2026-07-20T09:00:00+08:00")
        finally:
            if db:
                db.close()
            tmp.cleanup()

    def test_thresholds_are_configurable(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        db = None
        try:
            data_dir = Path(tmp.name) / "data"
            db = Database(data_dir / "radar.db")
            strict = Thresholds(strengthen_min=0.5, weaken_min=0.5)
            pipeline = RadarPipeline(
                db,
                load_profile("solid_state_battery"),
                thresholds=strict,
                artifact_root=data_dir / "artifacts",
            )
            task = load_demo_task()
            task["task_id"] = "task_strict"
            pipeline.create_task(task)
            pipeline.run(task["task_id"], mode="baseline", now="2026-07-28T09:00:00+08:00", period_days=40)
            update = pipeline.run(
                task["task_id"], mode="update", now="2026-08-08T09:00:00+08:00", period_days=14
            )
            # 提高门槛后，不应出现“增强/削弱”；证伪仍可发生（相反证据门槛独立）
            self.assertFalse(
                [c for c in update.changed if c.kind in {ChangeKind.STRENGTHEN, ChangeKind.WEAKEN}]
            )
            db.close()
        finally:
            if db:
                db.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
