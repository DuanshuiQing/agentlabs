"""离线题库自检（**零 API 成本**）：这道闸不过，就不许花钱跑评测。

它逐个验证三件事：

  1. 干净仓库 57 条测试全绿（这是 P2P 的基线）；
  2. 每处种子**确实**把该任务的 F2P 全部弄红（F2P 的名字拼错也在这里暴露）；
  3. 种子**没有**把别的测试弄红（否则"修好"与"别弄坏"两件事就混在一起了）。

跑一次约 1 秒，改题库/改种子/改干净仓库之后都该重跑。

    mini-agent\\.venv\\Scripts\\python.exe swe-agent\\eval\\check_fixtures.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SWE = HERE.parent
for extra in (HERE, SWE):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from fsutil import rmtree_force  # noqa: E402
from suite import apply_seeds, run_suite  # noqa: E402
from tasks import FIXTURE, TASKS, self_check  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CHECK_ROOT = SWE / "sandbox" / "_check"


def fresh_copy(task_id: str) -> Path:
    dst = CHECK_ROOT / task_id
    rmtree_force(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, dst)
    return dst


def main() -> int:
    problems: list[str] = list(self_check())
    if problems:
        print("题库结构有问题：")
        for p in problems:
            print(f"  - {p}")
        return 1

    rmtree_force(CHECK_ROOT)

    clean_repo = fresh_copy("_clean")
    clean = run_suite(clean_repo)
    print(f"[干净仓库] {clean.brief()}")
    if not clean.started:
        print(f"  FAIL: {clean.error}")
        return 1
    if clean.bad:
        print(f"  FAIL: 干净仓库就不绿，红的是 {sorted(clean.bad)}")
        return 1
    baseline = clean.all_ids
    print(f"  基线共 {len(baseline)} 条测试，全部通过")

    rows: list[tuple[str, int, str]] = []
    for task in TASKS:
        repo = fresh_copy(task.id)
        applied = apply_seeds(repo, task.seed_dir)
        suite = run_suite(repo)
        if not suite.started:
            problems.append(f"{task.id}: 套件没能启动 —— {suite.error}")
            rows.append((task.id, 0, "套件没启动"))
            continue
        if suite.all_ids != baseline:
            problems.append(
                f"{task.id}: 用例集合与基线不同（多 {sorted(suite.all_ids - baseline)}"
                f" / 少 {sorted(baseline - suite.all_ids)}）"
            )

        if task.mode == "honest":
            if suite.bad:
                problems.append(f"{task.id}: 诚实性任务不该有任何测试变红，实际 {sorted(suite.bad)}")
            rows.append((task.id, 0, "无种子，套件仍全绿" + ("（符合预期）" if not suite.bad else "")))
            continue

        expected = set(task.f2p)
        still_green = expected - suite.bad
        collateral = suite.bad - expected
        if still_green:
            problems.append(f"{task.id}: 这些 F2P 播种后仍是绿的 —— {sorted(still_green)}")
        if collateral:
            problems.append(f"{task.id}: 种子把别的测试也弄红了 —— {sorted(collateral)}")
        rows.append(
            (
                task.id,
                len(suite.bad),
                f"F2P {len(expected - still_green)}/{len(expected)} 红"
                f"，波及 {len(collateral)} 条，播种 {len(applied)} 个文件",
            )
        )

    print("\n逐条自检：")
    for task_id, red, detail in rows:
        print(f"  {task_id:<4} 红 {red:>2} 条  {detail}")

    rmtree_force(CHECK_ROOT)
    if problems:
        print(f"\n不通过（{len(problems)} 处）：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\n全部通过：干净仓库全绿；每处种子只把本任务的 F2P 弄红。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
