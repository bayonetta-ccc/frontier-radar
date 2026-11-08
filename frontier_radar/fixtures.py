"""演示场景：一条命令重建可复现的 baseline → 更新 → 静默 三期运行。"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .db import Database
from .pipeline import PipelineResult, RadarPipeline

DEMO_TASK = Path("config/demo/solid_state_battery/task.json")

DEMO_RUNS = [
    {
        "label": "baseline",
        "now": "2026-07-28T09:00:00+08:00",
        "mode": "baseline",
        "period_days": 40,
        "auto_confirm": False,
        "confirm_after": False,
        "why": "首次追踪：建立初始判断，全部等待用户确认。",
    },
    {
        "label": "week-2",
        "now": "2026-08-08T09:00:00+08:00",
        "mode": "update",
        "period_days": 14,
        "auto_confirm": False,
        "confirm_after": False,
        "why": "十天后：识别增强 / 证伪；高风险修改停在待确认，等用户核对证据。",
    },
    {
        "label": "quiet",
        "now": "2026-08-15T09:00:00+08:00",
        "mode": "update",
        "period_days": 7,
        "auto_confirm": False,
        "confirm_after": False,
        "why": "再一周：只有重复转载，系统明确输出“本期无需更新”；上一期的待确认修改仍摆在那里等你判断。",
    },
]


@dataclass
class DemoOutput:
    task_id: str
    db_path: Path
    runs: list[PipelineResult]
    summary: str


def load_demo_task(path: str | Path = DEMO_TASK) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def reset_workspace(data_dir: str | Path = "data") -> None:
    root = Path(data_dir)
    for name in ("radar.db", "radar.db-wal", "radar.db-shm"):
        target = root / name
        if target.exists():
            target.unlink()
    artifacts = root / "artifacts"
    if artifacts.exists():
        shutil.rmtree(artifacts)
    web = root / "web"
    if web.exists():
        shutil.rmtree(web)


def run_demo(
    data_dir: str | Path = "data",
    profile: str = "solid_state_battery",
    fixtures_dir: str | Path = "config/fixtures",
    reset: bool = True,
    source_mode: str = "offline",
) -> DemoOutput:
    if reset:
        reset_workspace(data_dir)
    db_path = Path(data_dir) / "radar.db"
    db = Database(db_path)
    pipeline = RadarPipeline(
        db, profile, fixtures_dir=fixtures_dir, artifact_root=Path(data_dir) / "artifacts"
    )
    task = load_demo_task()
    pipeline.create_task(task)
    results: list[PipelineResult] = []
    for spec in DEMO_RUNS:
        result = pipeline.run(
            task["task_id"],
            mode=spec["mode"],
            source_mode=source_mode,
            now=spec["now"],
            period_days=spec.get("period_days", 7),
            auto_confirm=spec["auto_confirm"],
            confirm_after=spec.get("confirm_after", False),
        )
        results.append(result)
    claims = db.list_claims(task["task_id"])
    lines = [
        "演示运行完成：",
        *[
            f"  · {spec['now'][:10]}  {spec['label']:<9} 变化 {len(result.digest.changes)} "
            f"待确认 {sum(1 for c in result.digest.changes if c.requires_confirmation)} "
            f"— {spec['why']}"
            for spec, result in zip(DEMO_RUNS, results)
        ],
        f"  · 基线判断 {len(results[0].baseline_claims)} 个；当前判断 {len(claims)} 条："
        + "；".join(f"{c.claim_id.split('_')[-1]}={c.status.value}" for c in claims),
        f"  · 数据库 {db_path}",
        f"  · 打开界面：python -m frontier_radar serve --open",
    ]
    db.close()
    return DemoOutput(task_id=task["task_id"], db_path=db_path, runs=results, summary="\n".join(lines))


__all__ = ["DEMO_RUNS", "DemoOutput", "load_demo_task", "reset_workspace", "run_demo"]
