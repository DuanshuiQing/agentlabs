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
from tasks import _conclusion, TASKS, match, self_check  # noqa: E402


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
    # ⚠️ **只统计能解析出「结论：…」行的轨迹。**
    # 题面加了输出契约之后，老轨迹几乎全部被"没有结论行"判死。把它们混进来，
    # 矩阵会变成一片 `·`，于是"零重叠"变成一个假的好消息 —— 实测踩过：
    # 加完结论行第一次跑 `verify_offline` 报"重叠 0 处"，真相是 62 份轨迹里
    # 只有 2 份带结论行，矩阵几乎是空的。
    # **被排除的条数必须打印出来**，否则又是一次"静默缩小样本"。
    print("\n=== 特异性矩阵：每条轨迹的答案，在**每个**断言下的结果 ===")
    print("     （只列能匹配到任务、且答案带「结论：…」行的轨迹；· 不过，P 通过）\n")
    ids = [t.id for t in TASKS]
    print(f"{'轨迹 / 答案来自':<30}" + "".join(f"{i:>5}" for i in ids))
    print("-" * (30 + 5 * len(ids)))
    overlaps: list[str] = []
    skipped_no_conclusion = 0
    comparable = 0
    for run in runs:
        own = match(run["prompt"])
        if own is None:
            continue
        if _conclusion(run["answer"]) is None:
            skipped_no_conclusion += 1
            continue
        comparable += 1
        cells = []
        for task in TASKS:
            passed, _ = task.check(run["answer"], None)
            cells.append("P" if passed else "·")
            # 别的任务的断言在自己的答案上通过 —— 记下来
            if passed and task.id != own.id:
                overlaps.append(f"{own.id} 的答案 也让 {task.id} 的断言通过了")
        print(f"{run['file'] + ' (' + own.id + ')':<30}" + "".join(f"{c:>5}" for c in cells))
    print(f"\n可比轨迹 {comparable} 份；因「没有结论行」被排除 {skipped_no_conclusion} 份")

    print("\n断言重叠情况（别的任务的断言，在本任务答案上也通过）：")
    if overlaps:
        for item in sorted(set(overlaps)):
            print(f"  · {item}")
        print("  → 重叠本身不是 bug（我们总是用对应题面跑），但它说明这些断言")
        print("    **只能判断'某条任务的答案是否达标'，不能用来区分是哪条任务**。")
        print("    任务集扩到 20+ 条时，同一类事实要避免被两条任务重复度量。")
    else:
        print("  （无重叠）")

    # ---------------- 空矩阵守卫 ----------------
    # **"零重叠"和"矩阵不可信"在输出上长得一模一样。**
    # 实测踩过：给 24 条题面统一加了「结论行」要求之后，所有历史轨迹都没有结论行，
    # 于是断言几乎全部判失败 —— 矩阵一片 `·`、0 处重叠。那不是"判据变紧了"，
    # 是"判据和这批数据的格式不匹配"。零重叠只在**数据确实带着新格式**时才有意义。
    #
    # 判据不看"通过数"而看"**有多少条答案能被解析出结论行**"，因为那才是病因：
    # 通过数低可能只是因为题目变难了，结论行缺失才是格式不匹配的确定信号。
    matched = [r for r in runs if match(r["prompt"]) is not None]
    with_concl = sum(1 for r in matched if _conclusion(r["answer"]) is not None)
    if matched and with_concl * 2 < len(matched):
        print()
        print(f"⚠️ 矩阵不可信：{len(matched)} 份轨迹里只有 **{with_concl}** 份能解析出「结论：…」行。")
        print("   这说明判据和数据的格式不匹配（比如题面新加了输出契约，而历史答案没有），")
        print("   不是判据变紧了。此时上面那句「无重叠」没有任何意义 —— 大部分格子是被格式判死的。")
        print("   → 要拿重叠情况当证据，必须用**格式变更之后**跑出来的轨迹。")

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
