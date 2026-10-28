"""阶段 1 · 第 2 步（2b）：评测运行器。

跑 N 轮 × M 条任务，每次都是**独立、可复现、可复核**的一次运行。

## 为什么"跑 N 次"不是"把 N 次串起来"

重复测量的前提是**每次都是同一起点**。串在一个会话里跑，第 2 次运行时历史里
躺着第 1 次的答案，上下文还只增不减 —— 通过率虚高、成本失真、失败互相污染。

起点要独立的是**三样**东西：

| 层 | 要重置什么 | 谁负责 |
| --- | --- | --- |
| 对话 | 空 messages，只放本次任务 | `agent.run()` 天然如此 |
| 磁盘 | 所有任务产物（`PRODUCTS`） | 本文件的**全局前置清理** |
| 轨迹 | 上一次运行写下的 `run-*.jsonl` | `tools.py` 的 `RunTrace`（第 2a 步） |

## 为什么清理不按任务挑

`T4` 会写 `logs/answers.txt`，而**那个文件的内容就是 T1 的答案**（实测已入库过，
见 PLAN §5 第 26 条）。评测是**交错**跑的（T1..T5 → T1..T5），
所以"清 T4 的产物"在 T1 那一轮不会发生 —— T1 会读到 T4 留下的答案。

→ 每条运行前清掉 `PRODUCTS` 的**全部**，不是按任务名查表。

## 为什么还要"清完再查"

清单会**忘**。新增一个产物、忘了加进 `PRODUCTS`，后果不是当场报错，
而是跑完 25 次之后拿到一份被污染、且**看起来正常**的通过率。

所以清理之后再用 `git status` 复查一遍：**只允许新增 `logs/run-*.jsonl`**。
任何别的变更都意味着"还有个产物没被清掉"，当场拒绝开跑。

## 为什么结果 JSON 必须记 git rev

"通过率 0.8"如果没有代码版本，三个月后无法复核、也无法和消融实验对比。
本文件把 `HEAD` 和"工作区是否脏"一起写进结果。

## 交错顺序

    for round in range(n):
        for task in tasks:
            ...

不是 `T1 × N` 然后 `T2 × N`。理由：把网络抖动、限流、模型侧漂移
**摊到所有任务上**。先跑完 T1 的 5 次，那 5 次共享同一刻的运气。

## 用法（先看计划，不花钱）

    mini-agent\\.venv\\Scripts\\python.exe evals\\run_eval.py --plan

    # 真正跑（花钱）：2 轮 × 5 条 = 10 次运行
    mini-agent\\.venv\\Scripts\\python.exe evals\\run_eval.py --n 2

    # 只验证报告管线，零成本：把历史轨迹里的真实答案重放一遍
    mini-agent\\.venv\\Scripts\\python.exe evals\\run_eval.py --replay
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MINI = ROOT / "mini-agent"
RESULTS = HERE / "results"

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(HERE))
# `import agent` 要靠它 —— `mini-agent/` 不在 sys.path 里的话，
# `--plan` 和 `--replay` 都发现不了，因为它们不导入 agent，只有真跑才炸。
sys.path.insert(0, str(MINI))
from tasks import BY_ID, MINI_ROOT, PRODUCTS, TASKS, self_check  # noqa: E402

# 工作区里**允许**出现的变更：新增的轨迹。别的一律算"隔离没做好"。
_ONLY_ALLOWED = re.compile(r"^mini-agent/logs/run-\d{8}-\d{6}(?:-\d+)?\.jsonl$")
# 产物目录：它们是结果，不是"代码版本"的一部分。
_OUTPUT_PREFIXES = ("evals/results/",)


# ---------------------------------------------------------------------------
# git：版本标记 + 隔离复查
#
# 两个问题，两个作用域 —— 不能混：
#   * 隔离复查问"被测系统干净吗" → 只看 `mini-agent/`（那里才是模型的可读根）
#   * 版本标记问"这份结果对应哪版代码" → `mini-agent/` + `evals/` 都要看
# 曾经把两者合成一个函数，于是改了 `evals/run_eval.py` 之后结果 JSON 里的
# `git_dirty` 仍然是空 —— 那份通过率看起来像是对应 HEAD 的，其实不是。
# ---------------------------------------------------------------------------


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{done.stderr.strip()}")
    return done.stdout


def _changed_in(*pathspec: str) -> list[str]:
    """`git status --porcelain` 里属于这些路径的变更（含未跟踪）。"""
    paths: list[str] = []
    for line in _git("status", "--porcelain", *pathspec).splitlines():
        if not line.strip():
            continue
        # 格式是 `XY path`；重命名是 `XY old -> new`，取右边那个
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        paths.append(path)
    return paths


def changed_paths() -> list[str]:
    """**被测系统**内相对 HEAD 的所有变更路径。"""
    return _changed_in("mini-agent/")


def isolation_problems() -> list[str]:
    """清完产物之后，被测系统里不该剩任何"不是轨迹"的变更。"""
    return [p for p in changed_paths() if not _ONLY_ALLOWED.match(p)]


def code_dirty() -> list[str]:
    """**代码**与 HEAD 不一致的地方 —— 它决定这份结果能不能横向比。

    范围是 `mini-agent/` + `evals/`，排除结果文件和轨迹（那些是产物）。
    只查 `mini-agent/` 是不够的：改过评测脚本之后，通过率同样不再可比。
    """
    dirty = _changed_in("mini-agent/", "evals/")
    return [
        p
        for p in dirty
        if not _ONLY_ALLOWED.match(p) and not p.startswith(_OUTPUT_PREFIXES)
    ]


def clear_products() -> list[str]:
    """全局前置清理。返回真的删掉了哪些。"""
    removed: list[str] = []
    for rel in PRODUCTS:
        target = MINI_ROOT / rel
        if target.exists():
            target.unlink()
            removed.append(rel)
    return removed


# ---------------------------------------------------------------------------
# 报告管线（--replay 也走这里，所以它必须能脱离 API 工作）
# ---------------------------------------------------------------------------


def summarize(records: list[dict]) -> dict:
    """按任务、按「独立事实」两条线汇总。"""
    total = len(records)
    passed = sum(1 for r in records if r["assert_ok"])

    by_task: dict[str, dict] = {}
    for task in TASKS:
        mine = [r for r in records if r["task"] == task.id]
        if not mine:
            continue
        hits = sum(1 for r in mine if r["assert_ok"])
        by_task[task.id] = {
            "runs": len(mine),
            "pass": hits,
            "pass_rate": round(hits / len(mine), 3),
            "tokens_sum": sum(r["tokens"] for r in mine),
            "tokens_max": max(r["tokens"] for r in mine),
            "over_budget": sum(1 for r in mine if r["over_budget"]),
            "reasons": sorted({r["reason"] for r in mine}),
        }

    # 按 fact 归组 —— 5 条任务只覆盖 3 组独立事实（PLAN §5 第 24 条），
    # 把 5 条当成 5 个独立样本去算方差是错的。
    facts: dict[str, dict] = {}
    for task in TASKS:
        mine = [r for r in records if r["task"] == task.id]
        if not mine:
            continue
        slot = facts.setdefault(task.fact, {"tasks": [], "runs": 0, "pass": 0})
        slot["tasks"].append(task.id)
        slot["runs"] += len(mine)
        slot["pass"] += sum(1 for r in mine if r["assert_ok"])
    for slot in facts.values():
        slot["pass_rate"] = round(slot["pass"] / slot["runs"], 3)

    return {
        "total_runs": total,
        "pass": passed,
        "pass_rate": round(passed / total, 3) if total else None,
        "fail_reason": {
            r["reason"] for r in records if not r["assert_ok"]
        } if any(not r["assert_ok"] for r in records) else set(),
        "tokens_total": sum(r["tokens"] for r in records),
        "by_task": by_task,
        "facts": facts,
    }


def show_report(summary: dict) -> None:
    print(f"\n{'=' * 62}")
    print(f"总计 {summary['total_runs']} 次运行，断言通过 {summary['pass']} 次"
          f"（{summary['pass_rate']:.0%}），共 {summary['tokens_total']} tokens")
    print(f"{'=' * 62}")

    print("\n[按任务]")
    print(f"  {'任务':<5}{'通过':>8}{'tokens 合计':>12}{'最大':>8}{'超预算':>8}")
    for tid, s in summary["by_task"].items():
        print(f"  {tid:<5}{s['pass']}/{s['runs']:<4}"
              f"{s['tokens_sum']:>12}{s['tokens_max']:>8}{s['over_budget']:>8}")

    print("\n[按「独立事实」归组] ← 这才是可比较的粒度")
    for fact, s in summary["facts"].items():
        ids = "/".join(s["tasks"])
        print(f"  {fact:<16} {s['pass']}/{s['runs']:<4} ({s['pass_rate']:.0%})  来自 {ids}")
    print("  ↑ 同一组的任务不构成独立样本：T4 的答案必然包含 T1 的，")
    print("    T5 的必然包含 T2 的。算方差、算置信区间时按这一层算。")


# ---------------------------------------------------------------------------
# 一次运行
# ---------------------------------------------------------------------------


def one_run(task, index: int, max_steps: int, verbose: bool) -> dict:
    """一条任务的一次运行：清产物 → 复查 → 跑 → 断言 → 记一行。"""
    removed = clear_products()

    leftovers = isolation_problems()
    if leftovers:
        # 不清干净就别跑 —— 跑出来的数字不可信，而且**看起来是正常的**。
        raise RuntimeError(
            "前置清理之后工作区仍有非轨迹变更，隔离没做好：\n  "
            + "\n  ".join(leftovers)
            + f"\n（已清：{removed}；PRODUCTS={list(PRODUCTS)} —— 漏了哪一项？）"
        )

    import agent  # 延迟导入：--plan 不该把 .env / openai 拉进来

    started = datetime.now()
    result = agent.run(task.prompt, max_steps=max_steps, verbose=verbose)
    elapsed = (datetime.now() - started).total_seconds()

    # 断言拿到的是**答案文本**和**磁盘**，拿不到 Result —— 它不许知道 agent 的内部结构
    assert_ok, why = task.check(result.answer, MINI_ROOT)

    tokens = task_tokens(result.trace)
    record = {
        "index": index,
        "task": task.id,
        "fact": task.fact,
        "trace": result.trace,
        "ok": result.ok,
        "reason": result.reason,
        "steps": result.steps,
        "tokens": tokens,
        "budget_tokens": task.budget_tokens,
        "over_budget": tokens > task.budget_tokens,
        "assert_ok": assert_ok,
        "assert_why": why,
        "elapsed_s": round(elapsed, 2),
    }
    return record


def task_tokens(trace_name: str) -> int:
    """从轨迹里读这次运行的总 token。轨迹才是**唯一数据源** —— 别再累加一遍。"""
    path = MINI_ROOT / "logs" / trace_name
    if not path.exists():
        return 0
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if record.get("role") == "result":
                total = (record.get("usage_total") or {}).get("total_tokens", 0)
    return total


# ---------------------------------------------------------------------------
# --replay：零成本验证报告管线
# ---------------------------------------------------------------------------


def load_history() -> dict[str, list[dict]]:
    """把历史轨迹里的真实答案按任务 id 归组。"""
    from tasks import match  # noqa: PLC0415 —— 只有这条路径需要

    groups: dict[str, list[dict]] = {}
    for path in sorted((MINI_ROOT / "logs").glob("run-*.jsonl")):
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        head = next((x for x in lines if x.get("role") == "run"), None)
        tail = next((x for x in reversed(lines) if x.get("role") == "result"), None)
        if head is None or tail is None:
            continue
        task = match(head["content"])
        if task is None:
            continue
        groups.setdefault(task.id, []).append({
            "trace": path.name,
            "answer": tail.get("content") or "",
            "reason": tail.get("reason"),
            "steps": tail.get("step"),
            "tokens": (tail.get("usage_total") or {}).get("total_tokens", 0),
        })
    return groups


def replay(task, index: int, history: dict[str, list[dict]], max_steps: int) -> dict:
    """重放一条历史答案，走**真的**断言与**真的**汇总。

    它验证的是报告管线（断言 / 聚合 / JSON / 退出码），
    **不是 agent 行为** —— 这里一次 API 都不发。两者别混。
    """
    pool = history.get(task.id) or []
    if not pool:
        return None
    item = pool[(index - 1) % len(pool)]  # 历史答案不够就循环用
    assert_ok, why = task.check(item["answer"], None)  # root=None：离线，跳过磁盘检查
    return {
        "index": index,
        "task": task.id,
        "fact": task.fact,
        "trace": item["trace"],
        "ok": item["reason"] not in (None, "max_steps"),
        "reason": item["reason"],
        "steps": item["steps"],
        "tokens": item["tokens"],
        "budget_tokens": task.budget_tokens,
        "over_budget": item["tokens"] > task.budget_tokens,
        "assert_ok": assert_ok,
        "assert_why": why,
        "elapsed_s": 0.0,
    }


# ---------------------------------------------------------------------------
# --rejudge：改完断言之后，用**同一批轨迹**重判（零成本）
#
# 为什么这件事必须存在：断言可以事后重跑，**运行不能**。
# 判据错了却重跑一遍，等于为别人的 bug 付钱（上一次是 90053 tokens）。
# 所以原始结果 `real-*.json` 永不覆盖，重判另存 `rejudged-*.json`，
# 两份并排摆着 —— 改了哪条、改前改后各是什么，一目了然。
# ---------------------------------------------------------------------------


def answer_of_trace(trace_name: str) -> str:
    path = MINI / "logs" / trace_name
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("role") == "result":
            return record.get("content") or ""
    return ""


def rejudge(result_path: Path) -> int:
    old = json.loads(result_path.read_text(encoding="utf-8"))
    print(f"=== 重判 {result_path.name}（零 API 成本）===")
    print(f"原判定：{old['summary']['pass']}/{old['summary']['total_runs']}"
          f"（{old['summary']['pass_rate']:.0%}）\n")

    records: list[dict] = []
    flips: list[str] = []
    for run in old["runs"]:
        task = BY_ID.get(run["task"])
        if task is None:
            print(f"  跳过 {run['task']}：任务集里已经没有它了")
            continue
        # root=None：**磁盘那半查不了** —— 那次运行的 answers.txt 早被下一次
        # 前置清理删掉了。离线重判只能查答案文本，这一条要写进报告。
        assert_ok, why = task.check(answer_of_trace(run["trace"]), None)
        if assert_ok != run["assert_ok"]:
            flips.append(f"{run['task']} {run['trace']}：{run['assert_ok']} → {assert_ok}")
        records.append({**run, "assert_ok": assert_ok, "assert_why": why})

    summary = summarize(records)
    show_report(summary)

    print("\n[判定翻转]")
    if flips:
        for item in flips:
            print(f"  {item}")
    else:
        print("  （没有翻转 —— 断言改动没有影响这批数据）")
    if any(r["task"] == "T4" for r in records):
        print("\n⚠️ T4 那条**只重查了答案文本**：磁盘上的 logs/answers.txt 早已被"
              "\n   下一次前置清理删掉，离线无法复核。原始结果里的磁盘判定仍然有效。")

    payload = {
        **old,
        "mode": "rejudged",
        "rejudged_from": result_path.name,
        "rejudged_at": datetime.now().isoformat(timespec="seconds"),
        "git_rev": _git("rev-parse", "HEAD").strip(),
        "git_dirty": code_dirty(),
        "runs": records,
        "summary": {**summary, "fail_reason": sorted(summary["fail_reason"])},
        "flips": flips,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"rejudged-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"\n重判结果已写入：{out}")
    print(f"原始结果保留未动：{result_path.name}")
    return 0 if summary["pass"] == summary["total_runs"] else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def plan_only(tasks, n: int, max_steps: int, replay_mode: bool) -> int:
    problems = self_check()
    print(f"任务集自检：{'通过' if not problems else f'{len(problems)} 个问题'}")
    for item in problems:
        print(f"  ✗ {item}")

    print(f"\ngit rev   ：{_git('rev-parse', 'HEAD').strip()}")
    dirty = changed_paths()
    print(f"工作区    ：{len(dirty)} 个变更" if dirty else "工作区    ：干净")
    for path in dirty:
        mark = "（轨迹，允许）" if _ONLY_ALLOWED.match(path) else "← 需要先处理"
        print(f"  {path}  {mark}")

    print(f"\n全局前置清理清单 PRODUCTS = {list(PRODUCTS)}")
    print(f"最大步数  ：{max_steps}")
    print(f"模式      ：{'--replay（零成本，重放历史答案）' if replay_mode else '真实运行（花钱）'}")

    total = n * len(tasks)
    print(f"\n将执行 {n} 轮 × {len(tasks)} 条 = {total} 次运行，交错顺序：")
    for round_no in range(1, n + 1):
        print(f"  第 {round_no} 轮：{' → '.join(t.id for t in tasks)}")

    if not replay_mode:
        # 历史实测：9 次成功运行均值 2815 tokens；含那次 max_steps 的全部均值 7262。
        print(f"\n成本估算（按阶段 0 实测）：约 {total * 2815} tokens（顺利）～")
        print(f"                        {total * 7262} tokens（撞上 max_steps）")
        print("  撞上一次 max_steps 就是 47288 tokens ≈ 47 倍于最便宜的那次。")

    if problems:
        return 1
    if not replay_mode and any(not _ONLY_ALLOWED.match(p) for p in dirty):
        print("\n失败：工作区有非轨迹变更，先提交或清掉再跑（隔离会被污染）。")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 1 评测运行器")
    parser.add_argument("--n", type=int, default=5, help="每条任务跑几轮（默认 5）")
    parser.add_argument("--tasks", default="", help="只跑这几条，逗号分隔，如 T1,T3")
    parser.add_argument("--max-steps", type=int, default=10, help="单次运行的步数上限")
    parser.add_argument("--plan", action="store_true", help="只打印计划，不真跑")
    parser.add_argument("--replay", action="store_true", help="零成本：重放历史答案验证报告管线")
    parser.add_argument(
        "--rejudge",
        metavar="结果JSON",
        help="零成本：用同一批轨迹、按当前断言重判某份 real-*.json，另存 rejudged-*.json",
    )
    parser.add_argument("--verbose", action="store_true", help="打印 agent 的过程输出")
    args = parser.parse_args()

    if args.rejudge:
        target = Path(args.rejudge)
        if not target.is_absolute():
            target = (RESULTS / target) if (RESULTS / target).exists() else target
        if not target.exists():
            print(f"失败：找不到 {args.rejudge}")
            return 1
        return rejudge(target)

    wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
    tasks = [BY_ID[t] for t in wanted] if wanted else list(TASKS)
    unknown = [t for t in wanted if t not in BY_ID]
    if unknown:
        print(f"失败：没有这些任务 {unknown}；可选 {sorted(BY_ID)}")
        return 1
    if args.n < 1:
        print("失败：--n 至少是 1")
        return 1

    if args.plan:
        return plan_only(tasks, args.n, args.max_steps, args.replay)

    history = load_history() if args.replay else {}

    records: list[dict] = []
    started = datetime.now()
    # 交错跑：外层轮次，内层任务
    for round_no in range(1, args.n + 1):
        for task in tasks:
            index = (round_no - 1) * len(tasks) + tasks.index(task) + 1
            print(f"[{index:>2}/{args.n * len(tasks)}] 第 {round_no} 轮 {task.id} …", end="", flush=True)
            if args.replay:
                record = replay(task, index, history, args.max_steps)
                if record is None:
                    print(" 跳过（没有历史答案）")
                    continue
            else:
                try:
                    record = one_run(task, index, args.max_steps, args.verbose)
                except RuntimeError as exc:
                    print(f"\n失败：{exc}")
                    return 1
            records.append(record)
            mark = "✓" if record["assert_ok"] else "✗"
            print(f" {mark} {record['reason']:<10} {record['steps']} 步 "
                  f"{record['tokens']:>6} tokens")

    if not records:
        print("失败：一次也没跑成，什么也没测到。")
        return 1

    summary = summarize(records)
    show_report(summary)

    RESULTS.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "mode": "replay" if args.replay else "real",
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "git_rev": _git("rev-parse", "HEAD").strip(),
        # 只记"非轨迹、非结果文件"的变更：本次运行自己产生的 `run-*.jsonl` 是
        # **预期内**的，把它们算成 dirty 会让每份结果都看起来不干净，脏信号就废了。
        # 这个列表非空 = 被测代码或评测脚本与 HEAD 不一致，那份通过率不能直接横向比。
        "git_dirty": code_dirty(),
        "model": _model_name(),
        "max_steps": args.max_steps,
        "n": args.n,
        "tasks": [t.id for t in tasks],
        "products_cleared": list(PRODUCTS),
        "budget": {t.id: t.budget_tokens for t in tasks},
        "runs": records,
        "summary": {**summary, "fail_reason": sorted(summary["fail_reason"])},
    }
    out = RESULTS / f"{'replay' if args.replay else 'real'}-{started:%Y%m%d-%H%M%S}.json"
    # newline="\n" 不能省：`write_text` 默认是文本模式，Windows 上会把 `\n` 翻成
    # `\r\n`，于是结果 JSON 违反 `.gitattributes` 的 `*.json eol=lf`，
    # 提交时冒出一堆 "CRLF will be replaced by LF"。README 第 13 条。
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"\n结果已写入：{out}")

    return 0 if summary["pass"] == summary["total_runs"] else 1


def _model_name() -> str:
    try:
        import agent  # noqa: PLC0415

        return agent.MODEL
    except Exception:  # noqa: BLE001 —— 拿不到模型名不该让报告失败
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
