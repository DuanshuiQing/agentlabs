"""阶段 1 · 第 1 步：把每条验收任务从「人话判定」改成「可执行的断言」。

## 为什么这是阶段 1 的第一步

阶段 0 的判定全部写成人话，例如 T2 是「答出正确金额；答案只在 `budget-Q4.txt` 里」。
**这种句子机器判不了** —— 而评测的全部意义就是让机器替你跑 N 次、报出通过率。
判据不自动化，后面的一切（基线、消融、回归）都无从谈起。

## 一条任务的四要素（PLAN §1 第 2 条的要求）

    prompt   输入 —— 原样交给 agent 的那句话
    expect   期望结果 —— 给人看的人话（和 prompt 一起构成"题面"）
    method   判定方式 —— 精确匹配 / 单元测试（性质断言）/ LLM 裁判
    check    断言本身 —— 一个纯函数，输入只有"答案文本"和"磁盘根目录"

## 两条设计规矩

**① 断言不许知道 agent 的内部结构。**
   签名是 `check(answer: str, root: Path | None) -> (bool, str)` ——
   只吃答案文本和磁盘状态。它不 import agent、不看 `Result`、不碰 `messages`。
   这样断言可以在**没有跑过任何 API 调用**的情况下被单独验证（见 `verify_offline.py`）。

**② 一条都不能用"精确匹配"。**
   PLAN §1 允许三种判定方式，但我们实测过：同一道 T4 两次运行写出的内容
   一次 41 字符、一次 67 字符，**措辞不同、内容都对**（复盘 §6）。
   所以全部退化成**性质断言**（"答案里必须出现这些子串"）+ 必要时的磁盘检查。
   LLM 裁判暂时不用 —— 它引入新的不确定性，留到"子串真的判不了"的任务上再说。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# 断言的签名：答案文本 + 磁盘根目录（mini-agent/）。
# root 为 None 表示"离线核对历史轨迹"，此时依赖磁盘的检查要自觉跳过并说明。
Check = Callable[[str, "Path | None"], "tuple[bool, str]"]


@dataclass(frozen=True)
class Task:
    """一条评测任务。"""

    id: str
    prompt: str  # 输入：原样交给 agent
    expect: str  # 期望结果：人话，给人看
    method: str  # 判定方式
    check: Check  # 断言本身
    fixtures: tuple[str, ...] = ()  # 依赖哪些 fixture（相对 mini-agent/）
    reset: tuple[str, ...] = ()  # 跑之前要清掉哪些历史产物（测试隔离）
    # ⚠️ 第 2 步（运行器）要改的一件事：`reset` 现在按任务挂，单条手工跑时够用，
    # 但评测是交错的（T1..T5 → T1..T5），T4 写下的 `logs/answers.txt` 会留在盘上，
    # 下一条 T1 就能读到它 —— 而它的内容正是 T1 的答案（见 PLAN §5 第 26、27 条）。
    # 正解：**每条运行前做一次全局前置清理**，把"哪些是任务产物"和"哪条任务"解耦。
    # 这里先留着现状，是因为改成全局会动到下面的 self_check 与 verify_offline。
    budget_tokens: int = 8000  # 成本预算：超过就是异常，要在报告里标出来


# ---------------------------------------------------------------------------
# 各任务的断言
# ---------------------------------------------------------------------------


def _t1(answer: str, root: Path | None) -> tuple[bool, str]:
    """T1：答出日志文件名。含 `run-` 和 `.jsonl` 两个特征串。"""
    hit = "run-" in answer and ".jsonl" in answer
    return hit, "含 'run-' 与 '.jsonl'" if hit else "答案里找不到完整的文件名特征"


# T3 的反例特征：handbook.txt 的**正文**串。
# 用途是抓一种具体的失败模式 —— 模型没读到 v2，于是把"猜到的另一个文件"总结了一遍
# （第 5 步实测出现过一次）。只查文件名不算违规，查到正文算。
_HANDBOOK_MARKERS = ("依赖与风格", "运行与日志", "LangChain")


def _t2(answer: str, root: Path | None) -> tuple[bool, str]:
    """T2：答出唯一金额 3800。"""
    hit = "3800" in answer
    return hit, "含 '3800'" if hit else "答案里没有 3800"


def _t3(answer: str, root: Path | None) -> tuple[bool, str]:
    """T3：如实说不存在，**且不得给出任何"内容总结"**。

    "不编造"没法直接断言，但可以断言它的反面证据：
    答案里不许出现 handbook.txt 的正文串（那说明它总结了别的文件）。
    """
    said = "不存在" in answer
    leaked = [m for m in _HANDBOOK_MARKERS if m in answer]
    if not said:
        return False, "没有如实说'不存在'"
    if leaked:
        return False, f"出现 handbook.txt 正文串 {leaked} —— 疑似总结错了文件"
    return True, "如实说不存在，且未带出其它文件的正文"


def _t4(answer: str, root: Path | None) -> tuple[bool, str]:
    """T4：写入 + 读回校验。**唯一一条需要看磁盘的**。

    离线核对时 root=None：只能查答案文本，磁盘那半必须当场查（文件会被下次重置清掉）。
    """
    text_ok = "run-" in answer and ".jsonl" in answer
    if root is None:
        return text_ok, "离线模式：只查了答案文本，未查磁盘（需运行后立即查）"

    target = root / "logs" / "answers.txt"
    if not target.exists():
        return False, "logs/answers.txt 不存在"
    content = target.read_text(encoding="utf-8")
    disk_ok = "run-" in content and ".jsonl" in content
    if not (text_ok and disk_ok):
        return False, f"文本={text_ok} 磁盘={disk_ok}；磁盘内容={content.strip()[:40]!r}"
    return True, f"磁盘内容={content.strip()[:40]!r}"


def _t5(answer: str, root: Path | None) -> tuple[bool, str]:
    """T5：7:5:3 分账。三个数字一个都不能错。

    这是"精确匹配"最接近的形态，但仍然用子串 —— 因为模型会在数字外面
    包一层 Markdown 表格、加粗、单位，逐字比对必然失败。
    """
    want = ("1773.33", "1266.67", "760")
    missing = [w for w in want if w not in answer]
    return not missing, "三个金额都在" if not missing else f"缺少 {missing}"


# ---------------------------------------------------------------------------
# 任务集
# ---------------------------------------------------------------------------

TASKS: tuple[Task, ...] = (
    Task(
        id="T1",
        prompt="读取 data/handbook.txt，回答：项目约定的日志文件名是什么？",
        expect="答出含 `run-` 和 `.jsonl` 的文件名，不编造其他内容",
        method="性质断言（子串）",
        check=_t1,
        fixtures=("data/handbook.txt",),
    ),
    Task(
        id="T2",
        prompt="在 data/ 目录里找出记录了本季度 API 预算的那个文件，告诉我金额。",
        expect="答出正确金额 3800；答案只在 budget-Q4.txt 里",
        method="性质断言（子串）",
        check=_t2,
        fixtures=("data/budget-Q4.txt", "data/agenda-1020.txt", "data/roster.txt"),
    ),
    Task(
        id="T3",
        prompt="读取 data/handbook-v2.txt 并总结它的内容。",
        expect="如实说不存在；编造出任何「内容」即为失败",
        method="性质断言（子串 + 反例排除）",
        check=_t3,
        fixtures=(),  # 这个 fixture 就是"不创建"
    ),
    Task(
        id="T4",
        prompt=(
            "读取 data/handbook.txt，把「项目约定的日志文件名」写入 logs/answers.txt，"
            "然后读回来确认写对了。"
        ),
        expect="logs/answers.txt 确实存在且内容正确；agent 报告了校验结果",
        method="性质断言（子串）+ **磁盘单元测试**",
        check=_t4,
        fixtures=("data/handbook.txt",),
        reset=("logs/answers.txt",),
    ),
    Task(
        id="T5",
        prompt=(
            "读取 data/budget-Q4.txt，把这笔预算按 7:5:3 分成三份，"
            "告诉我每份多少元（保留两位小数）。"
        ),
        expect="1773.33 / 1266.67 / 760 三个金额都正确",
        method="性质断言（子串 × 3）",
        check=_t5,
        fixtures=("data/budget-Q4.txt",),
        budget_tokens=6000,
    ),
)

BY_ID: dict[str, Task] = {t.id: t for t in TASKS}
BY_PROMPT: dict[str, Task] = {t.prompt: t for t in TASKS}

# 题面改过、但历史轨迹还要拿来核对的任务。
# T4 的原题面引用了内部编号"T1"，实测让模型烧掉 47288 tokens（PLAN §2 裁定 8）。
# 那条失败轨迹是**最有价值的证据之一**，不能因为它对应不上现题面就丢掉。
LEGACY_PROMPTS: dict[str, str] = {
    "把 T1 的答案写入 logs/answers.txt，然后读回来确认写对了。": "T4",
}


def match(prompt: str) -> Task | None:
    """按题面找任务。找不到就返回 None（历史轨迹用旧题面时会这样）。"""
    task = BY_PROMPT.get(prompt)
    if task is None:
        legacy = LEGACY_PROMPTS.get(prompt)
        task = BY_ID.get(legacy) if legacy else None
    return task


# ---------------------------------------------------------------------------
# 任务集自检：把"任务集的规格"也变成可执行断言
#
# 和 check_data.py 守 fixture 是同一个道理 —— 任务集是**会被人手改**的，
# 改坏了（id 撞了、fixture 写错路径、reset 把 fixture 删了）不会当场报错，
# 只会在跑完 N 次之后给你一个看不懂的通过率。
# ---------------------------------------------------------------------------

MINI_ROOT = Path(__file__).resolve().parent.parent / "mini-agent"


def self_check() -> list[str]:
    """检查任务集自身的结构不变量。返回问题列表，空列表表示全过。"""
    problems: list[str] = []

    ids = [t.id for t in TASKS]
    if len(set(ids)) != len(ids):
        problems.append(f"任务 id 有重复：{ids}")
    prompts = [t.prompt for t in TASKS]
    if len(set(prompts)) != len(prompts):
        problems.append("有两条任务的题面完全相同")

    for task in TASKS:
        if not task.prompt.strip() or not task.expect.strip():
            problems.append(f"{task.id} 的 prompt 或 expect 是空的")
        if not task.method.strip():
            problems.append(f"{task.id} 没写判定方式")
        if task.budget_tokens <= 0:
            problems.append(f"{task.id} 的成本预算不是正数")

        for rel in task.fixtures:
            if not (MINI_ROOT / rel).exists():
                problems.append(f"{task.id} 依赖的 fixture 不存在：{rel}")
            if rel.startswith("logs/run-") and rel.endswith(".jsonl"):
                # 轨迹是**产物**，不是 fixture。把它写进 fixtures 意味着
                # "这条任务依赖某次历史运行存在"，那评测就不可复现了。
                problems.append(f"{task.id} 把轨迹文件当成了 fixture：{rel}")

        for rel in task.reset:
            if rel in task.fixtures:
                problems.append(f"{task.id} 的 reset 会清掉自己的 fixture：{rel}")
            if not rel.startswith(("logs/", ".tmp/")):
                problems.append(f"{task.id} 的 reset 指向了不该清的路径：{rel}")

    return problems
