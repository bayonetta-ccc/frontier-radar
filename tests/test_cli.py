"""命令行接口测试：覆盖 demo / run / claims / feedback / metrics / 导出 / 服务和校验。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from frontier_radar.cli import main


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = str(Path(self.tmp.name) / "data")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = main(["--data-dir", self.data_dir, *args])
        return code, buffer.getvalue()

    def test_demo_and_claims(self) -> None:
        code, output = self.run_cli("--quiet", "demo")
        self.assertEqual(code, 0, output)
        self.assertIn("演示运行完成", output)

        code, output = self.run_cli("claims", "list", "--pending")
        self.assertEqual(code, 0, output)
        self.assertIn("待确认", output)

    def test_validate_and_metrics_json(self) -> None:
        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)
        self.assertIn("校验通过", output)

        self.run_cli("--quiet", "demo")
        code, output = self.run_cli("metrics", "--json")
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertIn("quality_gates", payload)
        self.assertTrue(payload["information_quality"]["link_integrity"] >= 0.99)

    def test_feedback_flow(self) -> None:
        self.run_cli("--quiet", "demo")
        code, output = self.run_cli(
            "feedback", "useful", "--claim", "toyota", "--note", "端到端测试"
        )
        self.assertEqual(code, 0, output)
        self.assertIn("已记录反馈", output)

    def test_claims_confirm_and_show(self) -> None:
        self.run_cli("--quiet", "demo")
        code, output = self.run_cli("claims", "list", "--pending")
        self.assertEqual(code, 0, output)
        # 从输出里取一个 claim_id
        claim_id = ""
        for line in output.splitlines():
            if line.startswith("clm_"):
                claim_id = line.split()[0]
                break
        self.assertTrue(claim_id, output)
        code, output = self.run_cli("claims", "show", claim_id)
        self.assertEqual(code, 0, output)
        self.assertIn("版本历史", output)
        code, output = self.run_cli("claims", "confirm", claim_id, "--note", "ok")
        self.assertEqual(code, 0, output)
        self.assertIn("已确认", output)

    def test_run_with_explicit_dates(self) -> None:
        code, output = self.run_cli("--quiet", "task", "new", "--id", "task_cli_case", "--topic", "固态电池")
        self.assertEqual(code, 0, output)
        code, output = self.run_cli(
            "run", "--task", "task_cli_case", "--mode", "baseline",
            "--now", "2026-07-28T09:00:00+08:00", "--period-days", "40",
        )
        self.assertEqual(code, 0, output)
        self.assertIn("初始基线", output)
        code, output = self.run_cli(
            "run", "--task", "task_cli_case",
            "--now", "2026-08-08T09:00:00+08:00", "--period-days", "14",
        )
        self.assertEqual(code, 0, output)
        self.assertIn("待确认", output)

    def test_task_show(self) -> None:
        self.run_cli("--quiet", "demo")
        code, output = self.run_cli("task", "show")
        self.assertEqual(code, 0, output)
        self.assertIn("task_solid_state_2026", output)
        self.assertIn("来源", output)


if __name__ == "__main__":
    unittest.main()
