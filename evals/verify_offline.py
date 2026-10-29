"""阶段 1 · 第 1 步的验证器：拿**已经落盘**的 10 份轨迹离线跑一遍断言。

零 API 成本。

## 为什么值得先做这个

**断言本身也是代码，也会错。** 在花 N×5 次 API 调用去跑基线之前，
先用真实答案把断言验一遍 —— 尤其是要确认它**能判失败**，
而不只是"对好东西点头"。（一个永远返回 True 的断言，跑一万次也只会
给你一个 100% 的通过率和零信息。）

## 它顺便回答一个问题：这些断言够不够"特异"

子串断言有个天然弱点：**它会互相重叠**。所以这里除了逐条核对，
还打一张"每条轨迹 × 每个断言"的矩阵 —— 看某个任务的断言会不会
在别的任务的答案上通过。重叠不一定是 bug（我们总是用对应的题面跑），
但它是**断言设计质量**的指标，也会影响后面消融实验的归因清晰度。

用法（纯本地，不发 API 请求）：
    mini-agent\\.venv\\Scripts\\python.exe evals\\verify_offline.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MINI = ROOT / "mini-agent"
LOGS = MINI / "logs"

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(HERE))
from tasks import TASKS, match, self_check  # noqa: E402


def load_runs() -> list[dict]:
    """把每份轨迹读成 {file, prompt, ok, reason, answer}。"""
    runs = []
    for path in sorted(LOGS.glob("run-*.jsonl")):
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        head = next((x for x in lines if x["role"] == "run"), None)
        tail = next((x for x in reversed(lines) if x["role"] == "result"), None)
        if head is None or tail is None:
            print(f"  [跳过] {path.name}：缺 run 行或 result 行")
            continue
        runs.append({
            "file": path.name,
            "prompt": head["content"],
            "ok": bool(tail.get("ok")),
            "reason": tail.get("reason"),
            "answer": tail.get("content") or "",
            "tokens": (tail.get("usage_total") or {}).get("total_tokens", 0),
        })
    return runs


def main() -> int:
    print("=== 任务集自检（结构不变量）===")
    structural = self_check()
    if structural:
        for item in structural:
            print(f"  ✗ {item}")
        print(f"\n任务集本身就有 {len(structural)} 个问题 —— 先修它，别急着跑评测。")
        return 1
    print(f"  OK：{len(TASKS)} 条任务，id/题面唯一，fixture 都在，reset 不误伤 fixture\n")

    runs = load_runs()
    print(f"=== 离线核对 {len(runs)} 份轨迹（零 API 成本）===\n")

    problems: list[str] = []
    unmatched: list[str] = []

    print(f"{'轨迹':<30}{'任务':<6}{'ok':<7}{'断言':<7}说明")
    print("-" * 108)
    for run in runs:
        task = match(run["prompt"])
        if task is None:
            unmatched.append(run["file"])
            print(f"{run['file']:<30}{'—':<6}{str(run['ok']):<7}{'—':<7}题面对不上任何任务")
            continue
        passed, note = task.check(run["answer"], None)
        mark = "PASS" if passed else "FAIL"
        print(f"{run['file']:<30}{task.id:<6}{str(run['ok']):<7}{mark:<7}{note[:46]}")
        # 结构性期望：agent 自己都宣告失败了，断言就必须判失败。
        # 一个在失败答案上也点头的断言，等于没有断言。
        if not run["ok"] and passed:
            problems.append(f"{run['file']}（{task.id}）agent 已宣告失败，断言却 PASS")
        # 反方向**不是**问题：agent 正常收尾（`ok=True`）但答案错了，
        # 恰恰是评测要抓的常态。原来这里也当成结构性问题拦下，是错的 ——
        # 阶段 1 第 4 步首次真跑里 T13 就出了这个状态（漏列一个文件），
        # 它是**真失败**，却被验证器报成"验证器自己坏了"。
        # 只有 `ok=False` 那一侧才是结构不变量：失败的运行绝不能被判通过。

    # ---------------- 特异性矩阵 ----------------
    print("\n=== 特异性矩阵：每条轨迹的答案，在**每个**断言下的结果 ===")
    print("     （只列能匹配到任务的轨迹；· 表示不过，P 表示通过）\n")
    ids = [t.id for t in TASKS]
    print(f"{'轨迹 / 答案来自':<30}" + "".join(f"{i:>5}" for i in ids))
    print("-" * (30 + 5 * len(ids)))
    overlaps: list[str] = []
    for run in runs:
        own = match(run["prompt"])
        if own is None:
            continue
        cells = []
        for task in TASKS:
            passed, _ = task.check(run["answer"], None)
            cells.append("P" if passed else "·")
            # 别的任务的断言在自己的答案上通过 —— 记下来
            if passed and task.id != own.id:
                overlaps.append(f"{own.id} 的答案 也让 {task.id} 的断言通过了")
        print(f"{run['file'] + ' (' + own.id + ')':<30}" + "".join(f"{c:>5}" for c in cells))

    print("\n断言重叠情况（别的任务的断言，在本任务答案上也通过）：")
    if overlaps:
        for item in sorted(set(overlaps)):
            print(f"  · {item}")
        print("  → 重叠本身不是 bug（我们总是用对应题面跑），但它说明这些断言")
        print("    **只能判断'某条任务的答案是否达标'，不能用来区分是哪条任务**。")
        print("    任务集扩到 20+ 条时，同一类事实要避免被两条任务重复度量。")
    else:
        print("  （无重叠）")

    # ---------------- 结论 ----------------
    print()
    if unmatched:
        print(f"未匹配到任务的轨迹 {len(unmatched)} 份：{unmatched}")
    if problems:
        print(f"\n失败：{len(problems)} 个结构性问题")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("结构检查通过：断言能判失败，也没有在下失败结论的轨迹上点头。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
