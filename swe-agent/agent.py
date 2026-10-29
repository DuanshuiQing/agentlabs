"""命令行入口：一次完整运行 = 建沙箱副本 → 跑循环 → 取 patch → 存档。

    # 手工试：直接给仓库和问题（最常用的两条）
    python swe-agent\\agent.py --repo swe-agent\\fixtures\\notes-app ^
        --issue "parse_duration('1h30m') 返回 1860，应为 5400"

    # 用题库里的任务（会自动播种 bug）
    python swe-agent\\agent.py --task S01
    python swe-agent\\agent.py --task S11          # 诚实性任务：期望 patch 为空

    # 换判定口径/预算
    python swe-agent\\agent.py --task S10 --max-steps 40 --budget-tokens 90000 --quiet
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _extra in (HERE, HERE / "eval"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import loop
import runlog
import sandbox
from tasks import BY_ID, FIXTURE  # noqa: E402 —— eval/tasks.py，题库是唯一数据源

ARMS = {
    "A0": loop.Features(),
    "A1": loop.Features(plan=True, label="A1"),
    "A2": loop.Features(reflect=True, label="A2"),
    "A3": loop.Features(compact=True, label="A3"),
    "A4": loop.Features(plan=True, reflect=True, compact=True, label="A4"),
}
# 三个模块在 M3 才接上；在它们真正生效之前不许用 A1–A4 跑，否则消融表是假的。
IMPLEMENTED_ARMS = {"A0"}


def main() -> int:
    parser = argparse.ArgumentParser(description="手动跑一条 SWE 任务")
    parser.add_argument("--repo", help="仓库路径（默认用 fixtures/notes-app）")
    parser.add_argument("--issue", default="", help="问题描述（用 --task 时自动取题库里的题面）")
    parser.add_argument("--task", help="题库 id，例如 S01")
    parser.add_argument("--arm", default="A0", choices=sorted(ARMS))
    parser.add_argument("--max-steps", type=int, default=loop.MAX_STEPS)
    parser.add_argument("--budget-tokens", type=int, default=loop.BUDGET_TOKENS)
    parser.add_argument("--wall-seconds", type=float, default=loop.WALL_SECONDS)
    parser.add_argument("--quiet", action="store_true", help="只打印结论，不打印每轮过程")
    cli = parser.parse_args()

    task_spec = BY_ID.get(cli.task) if cli.task else None
    if cli.task and task_spec is None:
        parser.error(f"没有这条任务: {cli.task}")
    issue = cli.issue or (task_spec.prompt if task_spec else "")
    if not issue:
        parser.error("要么给 --task，要么给 --issue")

    repo = Path(cli.repo).resolve() if cli.repo else FIXTURE
    if not repo.is_dir():
        parser.error(f"仓库不存在: {repo}")

    if cli.arm not in IMPLEMENTED_ARMS:
        parser.error(
            f"{cli.arm} 需要的模块还没接上（M3）；当前只有 {sorted(IMPLEMENTED_ARMS)} 可用。"
            " 现在就用别的臂跑，会把'没实现'误记成'没效果'。"
        )

    name = task_spec.id if task_spec else "manual"
    seed_files = task_spec.seed_dir if task_spec else None
    root = sandbox.prepare(name=name, source=repo, seed_files=seed_files)
    print(f"[沙箱] {root}")

    started = time.perf_counter()
    result = loop.run(
        issue,
        repo=root,
        max_steps=cli.max_steps,
        budget_tokens=cli.budget_tokens,
        wall_seconds=cli.wall_seconds,
        features=ARMS[cli.arm],
        verbose=not cli.quiet,
        task_id=task_spec.id if task_spec else "",
    )

    diff = sandbox.diff_of(root)
    status = sandbox.status_of(root)
    patch = runlog.save_patch(Path(result.trace).stem, diff)
    tampered = any(line[3:].replace("\\", "/").startswith("tests/") for line in status)

    print("\n===== 结果 =====")
    print(f"  ok        = {result.ok}")
    print(f"  reason    = {result.reason!r}")
    print(f"  steps     = {result.steps}")
    print(f"  tokens    = {result.tokens}")
    print(f"  tests     = 退出码 {result.tests_exit}，未通过 {result.tests_bad} 条")
    print(f"  工具调用  = {result.tool_calls}（错误 {len(result.errors)} 次）")
    print(f"  patch     = {patch}（{len(diff)} 字符；改动文件 {len(status)} 个）")
    print(f"  改了测试  = {tampered}")
    print(f"  轨迹      = {result.trace}")
    print(f"  耗时      = {time.perf_counter() - started:.1f}s")
    print(f"\n答案：\n{result.answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
