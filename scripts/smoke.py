"""一键自检：配置、测试、演示、指标与站点产物。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str]) -> int:
    print(f"\n$ {' '.join(args)}")
    result = subprocess.run([sys.executable, *args], cwd=ROOT)
    return result.returncode


def main() -> int:
    checks: list[tuple[str, int]] = []
    checks.append(("validate", run(["-m", "frontier_radar", "validate"])))
    checks.append(("tests", run(["-m", "unittest", "discover", "-s", "tests"])))
    checks.append(("demo", run(["-m", "frontier_radar", "--quiet", "demo"])))
    checks.append(("metrics", run(["-m", "frontier_radar", "metrics", "--json"])))

    web = ROOT / "data" / "web"
    required = ["index.html", "claims.html", "about.html"]
    missing = [name for name in required if not (web / name).exists()]
    reports = list((ROOT / "data" / "artifacts").glob("*/run_*/digest.md"))
    print("\n产物检查：")
    print(f"  静态站点：{web}（缺失 {missing or '无'}）")
    print(f"  报告数量：{len(reports)}")
    checks.append(("artifacts", 0 if not missing and len(reports) >= 3 else 1))

    failed = [name for name, code in checks if code != 0]
    print("\n结果：")
    for name, code in checks:
        print(f"  {'[OK]' if code == 0 else '[FAIL]'} {name}")
    if failed:
        print(f"\n失败项：{', '.join(failed)}")
        return 1
    print("\n全部通过。打开 data/web/index.html 或运行 python -m frontier_radar serve --open。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
