"""阶段 1 · 第 2 步：轨迹隔离的**事后审计** —— 证据层。

零 API 成本。

## 三层分工

| 层 | 问的问题 | 在哪 |
| --- | --- | --- |
| **机制** | 拦截生效了吗？（调用被拒） | `tools.py` 的 `_self_test` |
| **静态** | 仓库里有没有"天生就在"的泄漏？ | 本文件第 1 段 |
| **证据** | 历史上真的没发生过吗？ | 本文件第 2 段 |

自检证明不了后两件事：机制今天生效，不代表仓库是干净的，也不代表昨天那些轨迹是干净的。
**而"审计器零命中"和"审计器根本不工作"在输出上长得一模一样** ——
所以本文件的第一要务不是报零，是**先自证它会报警**。

## 静态检查：`logs/` 的入库白名单

第 1 段查 git 到底跟踪了 `logs/` 下哪些文件，白名单只有两类：

* `run-*.jsonl` —— 轨迹，**证据**，必须入库（AGENTS.md 硬规矩 2）
* `.gitkeep` —— 占位（git 不跟踪空目录）

多出第三种就是泄漏，而且是最隐蔽的一种：**已入库的文件在全新 clone 里天然存在**，
连"这次运行有没有留下它"都不用赌。

这条检查不是凭空的。它上线前抓到 `logs/answers.txt` 已入库，内容是
`项目约定的日志文件名：run-<时间戳>.jsonl（放在 logs/ 目录下）`
—— **那正是 T1 的答案原文**。也就是说，一个全新 clone 出来的仓库里，
模型 `read_file("logs/answers.txt")` 就能拿到 T1 的答案，完全不用碰 `handbook.txt`。

## 校准材料：阶段 0 的三份轨迹、23 处

跑之前我预期的阳性样本是 **1 份、4 处** —— 那是按**工具参数**扫出来的
（`read_file("logs/run-*.jsonl")` 恰好 4 次）。

按**观察文本**扫，实际是 **3 份、23 处**：

| 轨迹 | 处数 | 来源 |
| --- | --- | --- |
| `run-20261012-214245` | 10 | `read_file` 读了 4 份（含**它自己正在写的那份**）+ `list_dir` 列出 5 个名字 |
| `run-20261012-214320` | 6 | **全部**来自 `list_dir("logs")` |
| `run-20261012-214332` | 7 | **全部**来自 `list_dir("logs")` |

**多出来的 19 处全来自 `list_dir("logs")`** —— 那条通道的参数是干净的
（`{"path": "logs"}`），只在返回文本里露馅。这就是判据必须看观察文本的原因。

### 一个必须照实记录的事实

`214320` 和 `214332` 是**通过的 T4**，而它们确实调了 `list_dir("logs")`。
查过原始轨迹：那次 `list_dir("logs")` 和 `read_file("data/handbook.txt")` 是
**同一个 step 并行发出的两条 tool_call**，模型在看到任何结果之前就决定要读
handbook，最终答案也明确引用 handbook 第 5 条。**所以这两次通过没有被污染。**

但它暴露了断言的无能：**如果**某次运行只靠 `list_dir("logs")` 就答出
`run-<时间戳>.jsonl`，断言照样判通过 —— 它只查答案里有没有 `run-` 和
`.jsonl`，查不出答案**是怎么来的**。10 次运行里有 **3 次（30%）**真的调了
`list_dir("logs")`，只是这一次没被利用。

## 判据为什么是"观察文本"而不是"工具参数"

洞不止一条：

    read_file("logs/run-*.jsonl")   → 参数里就有轨迹名
    list_dir("logs")                → **参数干净**，但返回的目录列表里
                                      十个文件名个个都是 run-<时间戳>.jsonl

而 T1/T4 的答案**就是** `run-<时间戳>.jsonl`。所以只查参数会漏掉整个第二条洞
（那条洞更严重：它让题目从"读文件找那一行"退化成一步，判定还会显示"通过"）。

改成查**观察文本**（`role == "tool"` 的 `content`），两条洞一次覆盖。

用法（纯本地，不发 API 请求）：
    mini-agent\\.venv\\Scripts\\python.exe evals\\audit_isolation.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MINI = ROOT / "mini-agent"
LOGS = MINI / "logs"

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 只认**真实**时间戳形状。`handbook.txt` 里那句 "run-<时间戳>.jsonl" 是
# 占位符（T1/T4 的答案本身），不是轨迹名 —— 它不该被当成泄漏。
_TRACE_NAME = re.compile(r"run-\d{8}-\d{6}(?:-\d+)?\.jsonl")

# 拦截上线（阶段 1 第 2 步）之前产生的轨迹。**不许再增加。**
# 往名单里加一个名字，等于把一次真实的泄漏洗成"历史遗留"，
# 而这件事在 diff 里看起来只是一行字符串。
_LEGACY_TRACES = frozenset({
    "run-20261012-202119.jsonl",
    "run-20261012-203751.jsonl",
    "run-20261012-210137.jsonl",
    "run-20261012-213030.jsonl",
    "run-20261012-214245.jsonl",  # 10 处：read_file 4 次 + list_dir 5 个名字
    "run-20261012-214320.jsonl",  # 6 处：全是 list_dir("logs")，但这次通过没被污染
    "run-20261012-214332.jsonl",  # 7 处：同上
    "run-20261012-215529.jsonl",
    "run-20261013-210933.jsonl",
    "run-20261013-212734.jsonl",
})


def scan(path: Path) -> list[dict]:
    """扫一份轨迹的 **tool 观察**文本，返回所有真轨迹名的出现位置。"""
    hits: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("role") != "tool":
            continue
        text = record.get("content") or ""
        for found in _TRACE_NAME.finditer(text):
            start = max(0, found.start() - 24)
            hits.append({
                "step": record.get("step"),
                "tool": record.get("tool_name"),
                "name": found.group(0),
                "snip": text[start: found.end() + 24].replace("\n", " "),
            })
    return hits


def check_tracked() -> tuple[list[str], int]:
    """logs/ 的入库白名单：只许轨迹和 .gitkeep。返回 (问题列表, 跟踪文件数)。"""
    try:
        done = subprocess.run(
            ["git", "ls-files", "mini-agent/logs/"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError as exc:
        # 测不了也是失败 —— "什么也没测到"不能算通过
        return [f"跑不动 git（{exc}）—— 这项检查什么也没测到"], 0
    if done.returncode != 0:
        return [f"git ls-files 失败（退出码 {done.returncode}）：{done.stderr.strip()}"], 0

    tracked = [line for line in done.stdout.splitlines() if line.strip()]
    problems: list[str] = []
    for line in tracked:
        name = Path(line).name
        if name == ".gitkeep" or _TRACE_NAME.fullmatch(name):
            continue
        problems.append(f"{line} 已入库 —— 全新 clone 里它就摆在那儿，模型直接能读")
    return problems, len(tracked)


def main() -> int:
    print("=== 轨迹隔离审计 ===")
    print(f"轨迹目录：{LOGS}")
    print("判据：任何一条 tool 观察文本里出现**真实**的 run-<8位>-<6位>.jsonl")
    print("      → 说明模型当时看见了轨迹\n")

    # ---- 第 1 段：静态 —— 仓库里有没有"天生就在"的泄漏 -----------------
    print("[静态] logs/ 的入库白名单：只许 run-*.jsonl（证据）和 .gitkeep（占位）")
    tracked_problems, tracked_total = check_tracked()
    print(f"  git 跟踪了 {tracked_total} 个文件")
    if tracked_problems:
        for item in tracked_problems:
            print(f"  ✗ {item}")
    else:
        print("  ✓ 没有第三种文件")
    print()

    # ---- 第 2 段：证据 —— 历史轨迹的观察文本 ---------------------------
    traces = sorted(LOGS.glob("run-*.jsonl"))
    if not traces:
        # "什么也没测到"必须算失败 —— 零命中和零输入不是一回事。
        print("失败：logs/ 里没有任何轨迹，第 2 段什么也没测到。")
        return 1

    legacy = [p for p in traces if p.name in _LEGACY_TRACES]
    fresh = [p for p in traces if p.name not in _LEGACY_TRACES]

    print(f"[历史样本] 拦截上线前产生的 {len(legacy)} 份 —— 只报告，不计入判定")
    legacy_total = 0
    for path in legacy:
        hits = scan(path)
        legacy_total += len(hits)
        if hits:
            print(f"  {path.name}  {len(hits)} 处")
            for h in hits:
                print(f"      step={h['step']} {h['tool']} → {h['name']}")
                print(f"        ...{h['snip']}...")
    if legacy_total == 0:
        print("  全部 0 处")
    print(f"  合计 {legacy_total} 处\n")

    # ---- 校准：先证明这把尺子能量出东西 ------------------------------
    if legacy_total == 0:
        print("失败：审计器在已知阳性样本上也是 0 处 —— 它现在不可信。")
        print("      可能是判别式写错了，也可能是 _LEGACY_TRACES 里的样本被换掉了。")
        print("      在修好它之前，下面的\"新轨迹 0 处\"不能算证据。")
        return 1
    print(f"校准：✓ 在已知阳性样本上报出 {legacy_total} 处 —— 这把尺子确实会报警。")
    print("      预期 23 处（名单里 3 份有命中）。少于预期说明判别式漏了某条通道，")
    print("      多于预期说明出现了新的泄漏方式。两种都该停下来看一眼。\n")

    # ---- 新轨迹：硬约束 ----------------------------------------------
    print(f"[新轨迹] 拦截上线后产生的 {len(fresh)} 份 —— 命中必须为 0")
    problems: list[str] = []
    for path in fresh:
        hits = scan(path)
        if hits:
            problems.append(f"{path.name}：{len(hits)} 处")
            for h in hits:
                print(f"  ✗ {path.name} step={h['step']} {h['tool']} → {h['name']}")
        else:
            print(f"  {path.name}  0 处")
    if not fresh:
        print("  （还没有新轨迹 —— 第 3 步跑完基线后这里才会有东西）")

    print()
    if tracked_problems:
        print(f"失败：静态检查有 {len(tracked_problems)} 个问题（见上）。")
        print("      已入库的文件在全新 clone 里天然存在，必须先 gitignore 再 git rm --cached。")
        return 1
    if problems:
        print(f"失败：{len(problems)} 份新轨迹泄漏了运行轨迹。隔离没生效，这批数据不能用。")
        for item in problems:
            print(f"  ✗ {item}")
        return 1

    if not fresh:
        print("结论：静态检查通过；机制已上线，但还没有新轨迹可供审计 —— 这次只完成了校准。")
    else:
        print(f"结论：静态检查通过；{len(fresh)} 份新轨迹全部 0 处，隔离生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
