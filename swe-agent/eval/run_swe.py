"""评测运行器：交错跑题库 × 重复次数 × 消融臂，出结果 JSON。

沿用 `evals/run_eval.py`（阶段 1）已经验证过的四条规矩：

1. **交错顺序**：外层轮次 × 内层任务 —— 把"某一时段模型变慢/变贵"摊到所有任务上，
   而不是让某几条任务独占最贵的那一小时。
2. **全局前置清理**：每条运行前重建沙箱副本，不是按任务名查表。
3. **原始结果永不覆盖**：`real-*.json` 一次写一份；判据错了用 `--rejudge` 重判，
   **不重跑**（重跑花的是真钱，重判花的是 1 秒）。
4. **每个数字都能复核**：结果 JSON 里带 `git rev`、模型名、trace 名、patch 名。

用法：

    python swe-agent\\eval\\run_swe.py --plan                 # 只看计划与预估成本，不发请求
    python swe-agent\\eval\\run_swe.py --n 1 --tasks S01,S02,S04,S11   # 4 条 pilot
    python swe-agent\\eval\\run_swe.py --n 2                  # 全量
    python swe-agent\\eval\\run_swe.py --rejudge eval/results/real-xxx.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SWE = HERE.parent
for _extra in (HERE, SWE):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import judge  # noqa: E402
import llm  # noqa: E402
import loop  # noqa: E402
import runlog  # noqa: E402
import sandbox  # noqa: E402
from fsutil import rmtree_force  # noqa: E402
from tasks import BY_ID, FIXTURE, TASKS, Task, self_check  # noqa: E402

RESULTS = HERE / "results"
ARMS = {"A0": loop.Features()}


# ---------------------------------------------------------------------------
# 环境自证
# ---------------------------------------------------------------------------
def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(SWE.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def environment() -> dict:
    rev = _git("rev-parse", "--short", "HEAD").stdout.strip()
    dirty_fixtures = bool(_git("status", "--porcelain", "swe-agent/fixtures").stdout.strip())
    dirty_seeds = bool(_git("status", "--porcelain", "swe-agent/tasks").stdout.strip())
    return {
        "git_rev": rev,
        "fixtures_dirty": dirty_fixtures,
        "seeds_dirty": dirty_seeds,
        "model": llm.MODEL,
        "python": sys.version.split()[0],
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }


def preflight() -> list[str]:
    problems = list(self_check())
    env = environment()
    if env["fixtures_dirty"]:
        problems.append("fixtures/ 有未提交的改动 —— 基线变了，所有数字不可比")
    if env["seeds_dirty"]:
        problems.append("tasks/ 有未提交的改动 —— 跑 `tasks/seeds.py --check` 看是不是漂移了")
    if not FIXTURE.is_dir():
        problems.append(f"干净仓库不存在: {FIXTURE}")
    return problems


# ---------------------------------------------------------------------------
# 单次运行
# ---------------------------------------------------------------------------
def one_run(task: Task, n_index: int, arm: str, opts: argparse.Namespace) -> dict:
    root = sandbox.prepare(name=task.id, source=FIXTURE, seed_files=task.seed_dir)
    t0 = time.perf_counter()
    result = loop.run(
        task.prompt,
        repo=root,
        max_steps=opts.max_steps,
        budget_tokens=opts.budget_tokens,
        wall_seconds=opts.wall_seconds,
        features=ARMS[arm],
        verbose=False,
        task_id=task.id,
    )
    patch_text = sandbox.full_patch(root)
    patch_name = runlog.save_patch(Path(result.trace).stem, patch_text)
    verdict = judge.judge_task(task, root, answer=result.answer, patch_text=patch_text)
    elapsed = time.perf_counter() - t0

    record = {
        "task_id": task.id,
        "rep": n_index,
        "arm": arm,
        "trace": result.trace,
        "patch": patch_name,
        "answer": result.answer,
        "ok_selfcheck": result.ok,
        "steps": result.steps,
        "reason": result.reason,
        "tokens": result.tokens,
        "tests_exit": result.tests_exit,
        "tests_bad": result.tests_bad,
        "tool_calls": result.tool_calls,
        "tool_errors": result.errors,
        "over_budget": result.tokens > task.budget_tokens,
        "verdict": verdict,
        "elapsed_s": round(elapsed, 1),
    }
    print(
        f"  {task.id} 第 {n_index} 次  resolve={verdict['resolve']!s:<5} "
        f"outcome={verdict['outcome']:<14} steps={result.steps:<2} tokens={result.tokens:<6} "
        f"{elapsed:.0f}s"
    )
    return record


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def summarize(records: list[dict], arm: str) -> dict:
    total = len(records)
    resolved = sum(1 for r in records if r["verdict"]["resolve"])
    tokens = sum(r["tokens"] for r in records)
    steps = sum(r["steps"] for r in records)
    outcomes = Counter(r["verdict"]["outcome"] for r in records)
    tampered = sum(1 for r in records if r["verdict"]["tests_tampered"])
    over = sum(1 for r in records if r["over_budget"])
    by_task: dict[str, dict] = {}
    for record in records:
        slot = by_task.setdefault(record["task_id"], {"resolved": 0, "runs": 0, "tokens": 0})
        slot["runs"] += 1
        slot["tokens"] += record["tokens"]
        slot["resolved"] += int(record["verdict"]["resolve"])
    return {
        "arm": arm,
        "runs": total,
        "resolved": resolved,
        "resolve_rate": round(resolved / total, 4) if total else None,
        "tokens_total": tokens,
        "tokens_per_run": round(tokens / total, 1) if total else None,
        "tokens_per_resolved": round(tokens / resolved, 1) if resolved else None,
        "steps_total": steps,
        "steps_per_run": round(steps / total, 2) if total else None,
        "outcomes": dict(sorted(outcomes.items(), key=lambda kv: -kv[1])),
        "tests_tampered": tampered,
        "over_budget": over,
        "by_task": dict(sorted(by_task.items())),
    }


def show_summary(summary: dict) -> None:
    print("\n===== 汇总 =====")
    print(f"  臂           = {summary['arm']}   运行 {summary['runs']} 次")
    rate = summary["resolve_rate"]
    print(f"  resolve      = {summary['resolved']}/{summary['runs']}（{rate:.1%}）" if rate is not None else "  resolve = -")
    print(f"  tokens       = 共 {summary['tokens_total']}，均值 {summary['tokens_per_run']}")
    print(f"  每个已解决   = {summary['tokens_per_resolved']} tokens")
    print(f"  平均步数     = {summary['steps_per_run']}")
    print(f"  结局分布     = {summary['outcomes']}")
    print(f"  改了测试     = {summary['tests_tampered']} 次；超预算 {summary['over_budget']} 次")
    print("\n  逐条：")
    for task_id, slot in summary["by_task"].items():
        rate = f"{slot['resolved']}/{slot['runs']}"
        print(f"    {task_id:<4} {rate:<5} {slot['tokens']:>7} tokens")


# ---------------------------------------------------------------------------
# 重判（零 API 成本）
# ---------------------------------------------------------------------------
def rejudge(result_path: Path) -> int:
    data = json.loads(result_path.read_text(encoding="utf-8"))
    records = data.get("runs") or []
    if not records:
        print(f"{result_path} 里没有 runs")
        return 1

    print(f"重判 {result_path.name}：{len(records)} 次运行，零 API 调用")
    flips = 0
    updated: list[dict] = []
    for record in records:
        task = BY_ID[record["task_id"]]
        patch_path = runlog.PATCH_DIR / record["patch"]
        patch_text = patch_path.read_text(encoding="utf-8") if patch_path.exists() else ""
        root = sandbox.prepare(name=task.id, source=FIXTURE, seed_files=task.seed_dir)
        if patch_text.strip():
            applied, why = sandbox.apply_patch(root, patch_path)
            if not applied:
                print(f"  ! {record['task_id']}: patch 应用失败: {why}")
        verdict = judge.judge_task(task, root, answer=record.get("answer", ""), patch_text=patch_text)
        before = record["verdict"]
        changed = bool(before.get("resolve")) != bool(verdict["resolve"])
        flips += int(changed)
        record = {**record, "verdict": verdict, "verdict_before": before}
        updated.append(record)
        flag = "  ← 翻转" if changed else ""
        print(
            f"  {record['task_id']}  {before.get('outcome')} → {verdict['outcome']}"
            f"  resolve {before.get('resolve')} → {verdict['resolve']}{flag}"
        )

    data["runs"] = updated
    data["mode"] = "rejudged"
    data["rejudged_at"] = datetime.now().isoformat(timespec="seconds")
    data["summary"] = summarize(updated, data.get("arm", "A0"))
    out = RESULTS / f"rejudged-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    show_summary(data["summary"])
    print(f"\n翻转 {flips} 条；结果写入 {out.name}")
    return 0


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------
def show_plan(tasks: list[Task], n: int, arm: str, opts: argparse.Namespace) -> None:
    print(f"臂 {arm}；任务 {len(tasks)} 条 × 重复 {n} 次 = {len(tasks) * n} 次运行")
    print(f"max_steps={opts.max_steps}  budget={opts.budget_tokens}  wall={opts.wall_seconds}s")
    print("\n任务清单：")
    for task in tasks:
        seed = "无种子" if not task.seed_dir.is_dir() else f"种子 {len(list(task.seed_dir.rglob('*.py')))} 个文件"
        print(f"  {task.id:<4} f2p={len(task.f2p):<2} {seed:<18} 预算 {task.budget_tokens}  {task.expect[:44]}")

    previous = sorted(RESULTS.glob("real-*.json"))
    if not previous:
        print("\n预估成本：没有历史结果，无法预估 —— 先跑 pilot（例如 --tasks S01,S02,S04,S11 --n 1）")
        return
    last = json.loads(previous[-1].read_text(encoding="utf-8"))
    summary = last.get("summary") or {}
    avg = summary.get("tokens_per_run")
    if not avg:
        print("\n预估成本：历史结果里没有 tokens_per_run")
        return
    print(
        f"\n预估成本（按 {previous[-1].name} 的均值 {avg} tokens/次）："
        f"约 {int(avg * len(tasks) * n)} tokens"
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def select_tasks(spec: str) -> list[Task]:
    if not spec or spec == "all":
        return list(TASKS)
    wanted = [x.strip().upper() for x in spec.split(",") if x.strip()]
    unknown = [x for x in wanted if x not in BY_ID]
    if unknown:
        raise SystemExit(f"未知任务: {unknown}")
    return [BY_ID[x] for x in wanted]


def main() -> int:
    parser = argparse.ArgumentParser(description="swe-agent 评测运行器")
    parser.add_argument("--n", type=int, default=1, help="每条跑几次（默认 1）")
    parser.add_argument("--tasks", default="all", help="逗号分隔的任务 id，默认全部")
    parser.add_argument("--arm", default="A0", choices=sorted(ARMS))
    parser.add_argument("--max-steps", type=int, default=loop.MAX_STEPS)
    parser.add_argument("--budget-tokens", type=int, default=loop.BUDGET_TOKENS)
    parser.add_argument("--wall-seconds", type=float, default=loop.WALL_SECONDS)
    parser.add_argument("--plan", action="store_true", help="只打印计划与预估成本，不发请求")
    parser.add_argument("--rejudge", metavar="RESULTS_JSON", help="用已归档的 patch 重判，零 API")
    cli = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)

    if cli.rejudge:
        return rejudge(Path(cli.rejudge))

    problems = preflight()
    if problems:
        print("开跑前就拒绝（别让有问题的环境产出一份看起来正常的通过率）：")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    tasks = select_tasks(cli.tasks)
    if cli.plan:
        show_plan(tasks, cli.n, cli.arm, cli)
        return 0

    env = environment()
    print(f"环境：git {env['git_rev']}  model {env['model']}  python {env['python']}")
    print(f"计划：{len(tasks)} 条 × {cli.n} 次，臂 {cli.arm}")

    # 全局前置清理：所有任务的沙箱副本一律重建（不是按任务名查表）
    rmtree_force(sandbox.SANDBOX_ROOT)

    records: list[dict] = []
    started = time.perf_counter()
    for n_index in range(1, cli.n + 1):
        print(f"\n--- 第 {n_index} 轮 ---")
        for task in tasks:
            records.append(one_run(task, n_index, cli.arm, cli))

    summary = summarize(records, cli.arm)
    payload = {
        "mode": "real",
        "env": env,
        "n": cli.n,
        "arm": cli.arm,
        "options": {
            "max_steps": cli.max_steps,
            "budget_tokens": cli.budget_tokens,
            "wall_seconds": cli.wall_seconds,
        },
        "tasks": [t.id for t in tasks],
        "runs": records,
        "summary": summary,
        "elapsed_s": round(time.perf_counter() - started, 1),
    }
    out = RESULTS / f"real-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    show_summary(summary)
    print(f"\n结果写入 {out}")
    print(f"总耗时 {payload['elapsed_s']}s；重判用 --rejudge {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
