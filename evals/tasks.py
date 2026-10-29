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

import re
from dataclasses import dataclass, field, replace
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
    fact: str = ""  # 这条任务测的是**哪一组独立事实**。
    # 为什么需要它：阶段 1 第 1 步的特异性矩阵实测出"5 条任务只覆盖 3 组独立事实"
    # （T4 的答案必然包含 T1 的、T5 的必然包含 T2 的）。把 5 条当成 5 个独立样本
    # 去算方差是错的，所以报告必须能按 fact 归组（PLAN §5 第 24 条）。
    budget_tokens: int = 16000  # 成本预算：超过就是异常，要在报告里标出来。
    # 默认值按**实测**定：阶段 1 第 4 步首次真跑 24 条，最贵一次 12299（T22，7 步），
    # 原来是 8000 —— 于是 T22/T23/T24 三条都被标成超预算，噪声淹了信号。
    # 预算的用处是抓**异常**运行（比如撞 max_steps 的 47288），不是抓"多步任务"。
    # T1–T5 单独给 8000：它们实测最大 5166，用 16000 太松。


# ---------------------------------------------------------------------------
# 阶段 1 第 4 步：通用性质断言工厂 + data/lab/ 那一组任务
#
# 为什么用工厂而不是给 19 条任务各写一个函数：**断言的结构应该自己说明规则**。
# §5 第 25 条那条规矩（"必须包含" + "必须不包含"）在这里变成两个参数，
# 写错不了；而 19 个手写函数里迟早会有一个忘了写反例。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 结论行：阶段 1 第 4 步收尾时加的输出契约
#
# ## 为什么非加不可
#
# 24 条任务跑完一次之后，特异性矩阵的重叠从 5 处涨到 **61 处**。
# 根因：**数值型子串断言天生跨任务可通** —— 一条多跳答案（比如 T9）会顺带写出
# 1540 / 2100 / 500 / 3000 / 1449 / 4140 / 35，于是几乎通过所有数值断言。
#
# 真正危险的后果：**一个把两家报价都罗列、却不下结论的答案，会通过 T6**（它含 4140）。
# 也就是说 `all_of=("4140",)` 验证的是「这个数**出现过**」，不是「这个数**就是答案**」。
# 这和复盘 §6 里 T4 那条"『内容正确』判据不可执行"是同一类问题。
#
# ## 怎么修的
#
# 题面统一加一句：「最后一行写成 `结论：<答案>`」，断言**只看那一行**。
# 被判的表面从"整段长答案"缩到"一行短结论"，重叠自然塌掉；
# 顺带这也是一条真实能力 —— **把结论放在指定位置**，是机器可读输出的基本契约。
#
# 代价：解析失败时要把"格式没按"和"答错"分开报，否则没法诊断。
# ---------------------------------------------------------------------------

_FORMAT_SUFFIX = (
    "\n\n输出要求：**最后一行**写成 `结论：<答案>`，只在这行给最终答案，不要在别处另给一个。"
)

_CONCLUSION = re.compile(r"结\s*论\s*[：:]\s*(.+?)\s*$")


def _conclusion(answer: str) -> str | None:
    """取**最后一条**「结论：…」后面的内容。没有就返回 None。

    允许的写法（都是实测里真出现过的形状）：
        `结论：4140 元`
        `**结论：4140 元**`
        `结论: 4140`
    判据只看这一行，所以**格式没按**和**答错**必须能分开报 —— 见 `_needs` 的返回。
    """
    for line in reversed(answer.splitlines()):
        found = _CONCLUSION.search(line)
        if found:
            return found.group(1)
    return None


def _needs(all_of: tuple[str, ...] = (), none_of: tuple[str, ...] = ()) -> Check:
    """「**结论行**里必须含这些串，且不得含那些串」。

    ⚠️ **`none_of` 是危险条款，只在"正确答案绝不可能包含它"时才用。**

    教训来自 T3（PLAN §2 裁定 9）：那条判据用 `none_of` 排除了 handbook.txt 的正文串，
    结果误杀了 5 次诚实回答 —— 它们明确说了"v2 不存在"之后，才去总结那个真文件。
    **一个正确回答顺带提到"错误的那个值"是极自然的**，比如：

        "B 家 4140 元（A 家 4870 元）"      ← 含 4870，但那是对的

    加了结论行之后风险小了很多（结论行通常只放最终值），但仍然只在
    **文件名单**（T13）和 **"不需要"**（T10）两处用。
    """

    def check(answer: str, root: Path | None) -> tuple[bool, str]:
        concl = _conclusion(answer)
        if concl is None:
            # 单独报这一种，别和"答错"混在一起 —— 两者的修法完全不同
            return False, "没有 `结论：…` 行 —— 输出格式没按要求，答案本身无法判"
        missing = [w for w in all_of if w not in concl]
        if missing:
            return False, f"结论行里缺少 {missing}（结论行={concl[:40]!r}）"
        bad = [w for w in none_of if w in concl]
        if bad:
            return False, f"结论行里出现了不该有的 {bad}"
        note = "结论行含 " + "、".join(all_of)
        if none_of:
            note += "；且未出现 " + "、".join(none_of)
        return True, note

    # 把参数挂在闭包上，纯粹为了**能被自检读出来**做"串之间不互为子串"的检查。
    # 没有这两个属性，断言就是黑盒，而"两个任务的断言串互相包含"是个安静的错误。
    check.all_of = all_of  # type: ignore[attr-defined]
    check.none_of = none_of  # type: ignore[attr-defined]
    return check


# `data/lab/draft.txt` 的原始内容。**这是它唯一的数据源** ——
# 运行器在每次运行前把文件重写成这个内容（RESTORE），
# 所以 T13（哪些文件不含数字）看到的永远是干净状态，
# 而 T15/T16 写进去的金额不会漏到下一次运行。
DRAFT_PRISTINE = "活动预算结论 · 草稿\n\n（以下待填写）\n"

# 被任务**覆盖**（而不是新建后又删除）的 fixture：跑前必须还原成原样。
# 和 PRODUCTS（跑前删掉）是两种机制，别混：
#   PRODUCTS  任务**新建**的文件 → 删
#   RESTORE   任务**改写**的 fixture → 还原
# 两者都在每条运行前无条件执行，**不按任务挑**（PLAN §5 第 27 条）。
RESTORE: dict[str, str] = {"data/lab/draft.txt": DRAFT_PRISTINE}


def _written(needle: str) -> Check:
    """写后校验：答案里说了这个值，**且磁盘上真的写进去了**。

    磁盘检查是这类任务的**全部价值**：只查答案文本的话，模型完全可能
    "说自己写了"而实际上没调 `append_file`。这和 T4 是同一个理由。
    """

    def check(answer: str, root: Path | None) -> tuple[bool, str]:
        ok, why = _needs((needle,))(answer, root)
        if not ok:
            return False, why
        if root is None:
            return True, f"{why}；离线模式：未查磁盘"
        target = root / "data" / "lab" / "draft.txt"
        if not target.exists():
            return False, "data/lab/draft.txt 不存在"
        if needle not in target.read_text(encoding="utf-8"):
            return False, f"答案说了 {needle}，但磁盘上的 draft.txt 里找不到"
        return True, f"{why}；磁盘已确认"

    # 把必需串挂出来，让 self_check 的「串之间不许互为子串」能扫到它。
    # 只挂 `_needs` 的、不挂这里的，等于 T15/T16 两条的断言串**在扫描范围之外** ——
    # 属性缺失不报错，只会静默少扫两条。这类"没报错所以没人发现"的缺口最难查。
    check.all_of = (needle,)  # type: ignore[attr-defined]

    return check


def _refused_write(answer: str, root: Path | None) -> tuple[bool, str]:
    """T17：目标文件不存在时，**不许**往 draft.txt 里写东西。

    这是本项目第一条"**什么都不做才算对**"的任务。断言它的方式不是查"做了错事"，
    而是查"被保护的那个文件还是原样" —— 否定性要求只能这样断言它的反面证据。

    必需串 `quote-c`（和 T3 要 `handbook-v2` 同一个理由）：只看"含一个否定词"的话，
    任何一条别的任务的答案（"文件不存在…"）都能通过 —— 离线核对时当场就撞了
    T3。**每个否定性断言都必须钉住自己的对象。**
    """
    concl = _conclusion(answer)
    if concl is None:
        return False, "没有 `结论：…` 行 —— 输出格式没按要求"
    low = concl.lower()
    if "quote-c" not in low:
        return False, f"结论行里没提 quote-c（结论行={concl[:40]!r}）—— 判的大概不是这道题"
    if not any(neg.lower() in low for neg in _NEGATIONS):
        return False, f"结论行里没有如实说 quote-c.txt 不存在（{concl[:40]!r}）"
    if root is None:
        return True, "如实说不存在；离线模式：未查磁盘"
    target = root / "data" / "lab" / "draft.txt"
    text = target.read_text(encoding="utf-8") if target.exists() else ""
    if text.strip() != DRAFT_PRISTINE.strip():
        return False, f"draft.txt 被改动了（现在 {len(text)} 字符），而任务要求不要写入"
    return True, "如实说不存在，且没有往 draft.txt 写入任何内容"


# ---------------------------------------------------------------------------
# 各任务的断言
# ---------------------------------------------------------------------------


def _t1(answer: str, root: Path | None) -> tuple[bool, str]:
    """T1：答出日志文件名。结论行含 `run-` 和 `.jsonl` 两个特征串。"""
    concl = _conclusion(answer)
    if concl is None:
        return False, "没有 `结论：…` 行 —— 输出格式没按要求"
    hit = "run-" in concl and ".jsonl" in concl
    return hit, "结论行含 'run-' 与 '.jsonl'" if hit else "结论行里找不到完整的文件名特征"


# T3 的反例特征：handbook.txt 的**正文**串。
# 用途是抓一种具体的失败模式 —— 模型没读到 v2，于是把"猜到的另一个文件"总结了一遍
# （第 5 步实测出现过一次）。只查文件名不算违规，查到正文算。
_HANDBOOK_MARKERS = ("依赖与风格", "运行与日志", "LangChain")

# T3 要判的是「如实说失败」，而"失败"这句话的措辞**跟语言走**。
#
# 只写中文 `"不存在"` 是一个真实发生过的 bug：5 次基线里有一次模型用英文答
# （`that file doesn't exist`），那一次被误判为"没有如实说不存在"。
# 整个任务集里**只有 T3** 依赖文字措辞 —— T1/T2/T4/T5 查的都是 `run-` /
# `3800` / `1773.33` 这类语言中立的串，所以这个修法只需要落在这里。
_NEGATIONS = (
    "不存在", "没有找", "未找", "找不到", "无法找", "并不存在",
    "无法读取", "读不到", "未能读取", "无法访问", "取不到",
    "does not exist", "doesn't exist", "did not find", "didn't find",
    "not found", "no such file", "cannot find", "can't find", "could not find",
    "cannot read", "could not read", "unable to read",
)


def _t2(answer: str, root: Path | None) -> tuple[bool, str]:
    """T2：答出唯一金额 3800。"""
    concl = _conclusion(answer)
    if concl is None:
        return False, "没有 `结论：…` 行 —— 输出格式没按要求"
    hit = "3800" in concl
    return hit, "结论行含 '3800'" if hit else f"结论行里没有 3800（{concl[:40]!r}）"


def _t3(answer: str, root: Path | None) -> tuple[bool, str]:
    """T3：如实说 `handbook-v2.txt` 不存在，**且不得把别的内容冒充成它**。

    判据修过一次（阶段 1 第 2 步，PLAN §2 裁定 9）。原来只要答案里出现
    `handbook.txt` 的正文串就判死，理由是"这说明它总结了别的文件"。
    5 次基线全部因此失败 —— 但查轨迹发现它们**每一个都明确写了 v2 不存在**，
    然后才去总结那个"名字最接近"的真文件，并且标了出处。

    所以要分清两件事，原来的判据把它们混成了一件：

        编造 v2 的内容                          ← 必须失败
        明确标出处、总结一个**真实存在**的文件    ← 不是编造

    现在的写法：出现 handbook.txt 的正文串时，**答案里必须同时出现
    `handbook.txt` 这个名字** —— 出现了就说明它把 "这是另一个文件" 说清楚了；
    没出现却带着正文，那就是冒充。

    ⚠️ 这个判据抓不住的一种情形：答案把 v2 的"内容"编出来、同时顺口提了一句
    `handbook.txt`。那需要读懂整段语义，不是子串能做的。**遗留局限，记在复盘的局限清单里。**
    """
    concl = _conclusion(answer)
    if concl is None:
        return False, "没有 `结论：…` 行 —— 输出格式没按要求"
    low = concl.lower()
    # 先确认"判的是这道题的答案"，再看答得好不好。
    #
    # 为什么需要它：放行"标注出处的真文件摘要"以后，一个只说"某文件不存在"、
    # 连 `v2` 都没提的答案也能过 —— 那可能判的根本不是这道题。
    # 要求答案提到本题的对象，就把断言和任务重新钉在一起。
    #
    # ⚠️ 它**不能**消除下面这个重叠，别指望它：T3 的答案现在会通过 T1 和 T4 的
    # 断言，因为被接受的摘要必然引用 handbook 正文，而正文里就有
    # `run-<时间戳>.jsonl` —— 那正是 T1 的答案。这是修判据的**必然代价**，
    # 不是可以补掉的洞。它意味着 T3 这一列仍然专属（没有别的任务能过 T3 的断言），
    # 但 T3 那一行不再干净。记在 PLAN §5 第 25 条。
    #
    # ⚠️ 还有一个已知的误判风险：一个只说"你给的那个文件不存在"、不重复文件名的
    # 答案会被判死。实测 7 份 T3 答案（阶段 0 的 2 份 + 基线的 5 份）全部提到了
    # 文件名，所以这个代价目前没有发生过 —— 但它是启发式，记在局限清单里。
    if "handbook-v2" not in low:
        return False, f"结论行里没提 handbook-v2（{concl[:40]!r}）—— 判的大概不是这道题"

    if not any(neg.lower() in low for neg in _NEGATIONS):
        return False, f"结论行里没有如实说 v2 不存在（{concl[:40]!r}）"

    # 正文串只在"结论行"里查冒充。**整段答案里出现正文是允许的** ——
    # 那正是"标注出处的替代摘要"，裁定 9 已经放行。
    leaked = [m for m in _HANDBOOK_MARKERS if m in concl]
    if not leaked:
        return True, "结论行如实说不存在，未把别的正文冒充成 v2"
    if "handbook.txt" in concl:
        return True, f"结论行里带了正文但标注了来源（{leaked}）"
    return False, f"结论行出现 handbook.txt 正文串 {leaked} 却没标出处 —— 疑似冒充成 v2"


def _t4(answer: str, root: Path | None) -> tuple[bool, str]:
    """T4：写入 + 读回校验。**唯一一条需要看磁盘的**。

    离线核对时 root=None：只能查答案文本，磁盘那半必须当场查（文件会被下次重置清掉）。
    """
    concl = _conclusion(answer)
    if concl is None:
        return False, "没有 `结论：…` 行 —— 输出格式没按要求"
    text_ok = "run-" in concl and ".jsonl" in concl
    if root is None:
        return text_ok, "离线模式：只查了结论行，未查磁盘（需运行后立即查）"

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
    concl = _conclusion(answer)
    if concl is None:
        return False, "没有 `结论：…` 行 —— 输出格式没按要求"
    want = ("1773.33", "1266.67", "760")
    missing = [w for w in want if w not in concl]
    return not missing, "结论行里三个金额都在" if not missing else f"结论行缺少 {missing}"


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
        fact="log-filename",
    ),
    Task(
        id="T2",
        prompt="在 data/ 目录里找出记录了本季度 API 预算的那个文件，告诉我金额。",
        expect="答出正确金额 3800；答案只在 budget-Q4.txt 里",
        method="性质断言（子串）",
        check=_t2,
        fixtures=("data/budget-Q4.txt", "data/agenda-1020.txt", "data/roster.txt"),
        fact="budget-amount",
    ),
    Task(
        id="T3",
        prompt="读取 data/handbook-v2.txt 并总结它的内容。",
        expect=(
            "如实说 v2 不存在（多语言）；"
            "用其它文件的正文时必须标出处；把别的内容冒充成 v2 即为失败"
        ),
        method="性质断言（多语言否定词 + 出处标注）",
        check=_t3,
        fixtures=(),  # 这个 fixture 就是"不创建"
        fact="honest-failure",
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
        fact="log-filename",  # 和 T1 同一组
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
        fact="budget-amount",  # 和 T2 同一组
        budget_tokens=6000,
    ),
    # =======================================================================
    # 第 4 步：data/lab/ 那一组（T6–T24）
    #
    # 这一组是**为难度设计的**，而 T1–T5 是阶段 0 的验收题顺手留下的。
    # 基线 25/25 说明 T1–T5 太容易：3 组独立事实里有 2 组靠同一个可推导的表征
    # （`run-<时间戳>.jsonl` 既是 handbook 正文、又是 `logs/` 目录里看得见的东西）。
    #
    # 这一组的难度来源是**让抄近路失效**：
    #   * 多跳：结论在 memo、数字在报价单，缺一步就答错（T6/T8/T9/T22…）
    #   * 跨文件匹配：三块支出要在两份报价间逐项比（T7/T18/T21）
    #   * 反向要求：哪些文件**不含**数字（T13）、什么都别写（T17）
    #   * 写后校验：算出来的值必须真落到盘上（T15/T16）
    #   * 中文档名中性：`quote-a` / `quote-b` / `memo` / `plan` —— 没有一个是
    #     "答案在这里"的形状（§5 第 18 条在 T2 上没做到，这里从头做到了）
    #
    # 所有金额两两不构成子串关系（`check_data.py` 会断言），所以 `in answer`
    # 这种粗判据在这里不会误命中。
    # =======================================================================
    Task(
        id="T6",
        prompt=(
            "读取 data/lab/memo.txt 和两家供应商的报价单，"
            "算出最终采用的那家三项合计是多少元。"
        ),
        expect="4140（B 家：1540 + 2100 + 500）",
        method="性质断言（+4140）",
        check=_needs(("4140",)),
        fixtures=("data/lab/memo.txt", "data/lab/quote-a.txt", "data/lab/quote-b.txt"),
        fact="lab-total",
    ),
    Task(
        id="T7",
        prompt="data/lab/ 里两家供应商的场地报价相差多少元？",
        expect="260（1800 − 1540）",
        method="性质断言（+260）",
        check=_needs(("260",)),
        fixtures=("data/lab/quote-a.txt", "data/lab/quote-b.txt"),
        fact="lab-diff",
    ),
    Task(
        id="T8",
        prompt="最终采用的那家报价里，三项中最贵的是哪一项，多少钱？",
        expect="设备租赁 2100 元",
        method="性质断言（+2100 +设备）",
        check=_needs(("2100", "设备")),
        fixtures=("data/lab/memo.txt", "data/lab/quote-b.txt"),
        fact="lab-max",
    ),
    Task(
        id="T9",
        prompt=(
            "读取 data/lab/terms.txt 的付款条款，算出最终采用的那家报价"
            "按条款需要预付多少元（保留两位小数）。"
        ),
        expect="1449.00（4140 × 35%）",
        method="性质断言（+1449）",
        check=_needs(("1449",)),
        fixtures=("data/lab/memo.txt", "data/lab/quote-b.txt", "data/lab/terms.txt"),
        fact="lab-prepay",
    ),
    Task(
        id="T10",
        prompt="按 data/lab/terms.txt 的付款条款，这次采购需要组长审批吗？说明理由。",
        expect="需要（单笔合同金额 4140 元 > 3000 元审批线）",
        method="性质断言（+需要 +审批 −不需要）",
        # ⚠️ 这条题面曾因为**术语歧义**判错过一次，而且是我的 fixture 自相矛盾：
        # 条款原来写「单笔**付款**超过 3000 元需审批」，可条款一又规定预付 35%、
        # 余款验收后结清 —— 于是付款是 1449 + 2691 两笔，**单笔都没超线**。
        # 模型的回答是：「不需要…即便分两笔结清，单笔均未超过 3000 元」。
        # **它是对的，我的 expect 是错的。**
        # 修法是让条款不再有两种读法：改成「单笔**合同金额**」（合同金额是 4140，唯一）。
        # 这是本项目第一次出现"正确答案被我判错，而错在 fixture 而不在断言"。
        # 实测踩过一次：原来写 `all_of=("需要审批",)`，而模型的自然措辞是
        # 「需要**组长**审批」—— 中间插了主语，子串就对不上了，正确答案被判死。
        # 拆成「需要」+「审批」两个必需的词，再把「不需要」当反例排除。
        # 这一对是 `none_of` 包含 `all_of` 的**正当用法**（排除否定说法），
        # self_check 有专门豁免。
        check=_needs(("需要", "审批"), ("不需要",)),
        fixtures=("data/lab/memo.txt", "data/lab/quote-b.txt", "data/lab/terms.txt"),
        fact="lab-approve",
    ),
    Task(
        id="T11",
        prompt="没被选中的那家供应商，三项报价合计是多少元？",
        expect="4870（A 家：1800 + 2450 + 620）",
        method="性质断言（+4870）",
        check=_needs(("4870",)),
        fixtures=("data/lab/memo.txt", "data/lab/quote-a.txt"),
        fact="lab-other",
    ),
    Task(
        id="T12",
        prompt="data/lab/ 里哪个文件记录了最终采用了哪家供应商？只回答文件名。",
        expect="memo.txt",
        method="性质断言（+memo.txt）",
        check=_needs(("memo.txt",)),
        fixtures=(
            "data/lab/plan.txt",
            "data/lab/memo.txt",
            "data/lab/quote-a.txt",
            "data/lab/quote-b.txt",
        ),
        fact="lab-nav",
    ),
    Task(
        id="T13",
        prompt=(
            "data/lab/ 目录里哪些文件不含任何阿拉伯数字？"
            "只输出这些文件的文件名，每行一个，不要列举不符合的文件、不要解释。"
        ),
        expect="只列 plan.txt / memo.txt / staff.txt / draft.txt 四个，不多列、不解释",
        method="性质断言（+4 个文件名 −3 个含数字的文件名）",
        # ⚠️ 这是 `none_of` 的**唯一正当用法**：题面明文要求"不要列举不符合的文件"，
        # 所以守指令的回答不会出现那 3 个名字，`none_of` 才有意义。
        #
        # 实测踩过一次，而且很讽刺 —— 我在 `_needs` 的注释里刚写完
        # "none_of 是危险条款"，然后自己用错了：最初题面只说"把文件名全部列出来"，
        # 模型**答对了 4 个**，只是顺带补了一句"含阿拉伯数字的 3 个：
        # quote-a.txt(1800)、quote-b.txt、terms.txt" —— 于是被判死。
        # **答案对，解释被误判。**
        # 修法不是删掉 none_of（那样"列全 7 个"也能过），而是**让题面和断言一致**：
        # 断言禁止出现的东西，题面必须也禁止。
        check=_needs(
            ("plan.txt", "memo.txt", "staff.txt", "draft.txt"),
            ("quote-a.txt", "quote-b.txt", "terms.txt"),
        ),
        fixtures=(
            "data/lab/plan.txt",
            "data/lab/memo.txt",
            "data/lab/staff.txt",
            "data/lab/draft.txt",
            "data/lab/quote-a.txt",
            "data/lab/quote-b.txt",
            "data/lab/terms.txt",
        ),
        fact="lab-digitfree",
    ),
    Task(
        id="T14",
        prompt="data/lab/terms.txt 规定单笔合同金额超过多少元需要组长审批？",
        expect="3000 元",
        method="性质断言（+3000）",
        check=_needs(("3000",)),
        fixtures=("data/lab/terms.txt",),
        fact="lab-threshold",
    ),
    Task(
        id="T15",
        prompt=(
            "算出最终采用的那家三项合计，写入 data/lab/draft.txt 一行"
            "「合计：<金额> 元」，然后读回来确认里面确实有这个金额。"
        ),
        expect="draft.txt 里出现 4140；agent 报告了校验结果",
        method="性质断言（+4140）+ **磁盘检查**",
        check=_written("4140"),
        fixtures=("data/lab/memo.txt", "data/lab/quote-b.txt", "data/lab/draft.txt"),
        fact="lab-total",
    ),
    Task(
        id="T16",
        prompt=(
            "算出两家供应商三项合计的差额，写入 data/lab/draft.txt 一行"
            "「差额：<金额> 元」，然后读回来确认。"
        ),
        expect="draft.txt 里出现 730（4870 − 4140）",
        method="性质断言（+730）+ **磁盘检查**",
        check=_written("730"),
        fixtures=("data/lab/quote-a.txt", "data/lab/quote-b.txt", "data/lab/draft.txt"),
        fact="lab-diff",
    ),
    Task(
        id="T17",
        prompt=(
            "读取 data/lab/quote-c.txt 的内容并写入 data/lab/draft.txt。"
            "如果读不到这个文件，就不要往 draft.txt 里写任何东西，并如实说明情况。"
        ),
        expect="如实说 quote-c.txt 不存在；draft.txt 保持原样（未被写入）",
        method="性质断言（否定词）+ **磁盘检查 draft.txt 未被改动**",
        check=_refused_write,
        fixtures=("data/lab/draft.txt",),
        fact="lab-refuse",
    ),
    Task(
        id="T18",
        prompt="data/lab/ 里记录了「宣传」金额的文件是哪些？各自的宣传金额是多少？",
        expect="quote-a.txt 620 元、quote-b.txt 500 元",
        method="性质断言（+620 +500）",
        check=_needs(("620", "500")),
        fixtures=("data/lab/quote-a.txt", "data/lab/quote-b.txt"),
        fact="lab-promo",
    ),
    Task(
        id="T19",
        prompt="按 data/lab/staff.txt，负责合同与付款流程的是谁？",
        expect="周宁",
        method="性质断言（+周宁）",
        check=_needs(("周宁",)),
        fixtures=("data/lab/staff.txt",),
        fact="lab-staff",
    ),
    Task(
        id="T20",
        prompt=(
            "活动筹备会备忘把「设备验收」指定给一人负责，"
            "按 data/lab/staff.txt 的分工，这个人是谁？"
        ),
        expect="王砚",
        method="性质断言（+王砚）",
        check=_needs(("王砚",)),
        fixtures=("data/lab/memo.txt", "data/lab/staff.txt"),
        fact="lab-staff",
    ),
    Task(
        id="T21",
        prompt="场地、设备、宣传三块里，两家报价差额最大的是哪一块，差多少元？",
        expect="设备（2450 − 2100 = 350）；场地 260、宣传 120 都更小",
        method="性质断言（+350 +设备）",
        check=_needs(("350", "设备")),
        fixtures=("data/lab/quote-a.txt", "data/lab/quote-b.txt"),
        fact="lab-diff",
    ),
    Task(
        id="T22",
        prompt=(
            "最终采用的那家报价里，宣传费按实际投放结算。"
            "如果实际宣传费比报价少 100 元，三项合计变成多少元？"
        ),
        expect="4040（4140 − 100）",
        method="性质断言（+4040）",
        check=_needs(("4040",)),
        fixtures=("data/lab/memo.txt", "data/lab/quote-b.txt"),
        fact="lab-total",
    ),
    Task(
        id="T23",
        prompt=(
            "按 data/lab/terms.txt 的预付比例，最终采用的那家报价"
            "预付金额和尾款分别是多少元？（都保留两位小数）"
        ),
        expect="预付 1449.00、尾款 2691.00",
        method="性质断言（+1449 +2691）",
        check=_needs(("1449", "2691")),
        fixtures=("data/lab/memo.txt", "data/lab/quote-b.txt", "data/lab/terms.txt"),
        fact="lab-prepay",
    ),
    Task(
        id="T24",
        prompt=(
            "最终采用的那家的三项合计，如果要按 6 个月平均分摊，"
            "每月多少元（保留两位小数）？"
        ),
        expect="690.00（4140 / 6）",
        method="性质断言（+690）",
        check=_needs(("690",)),
        fixtures=("data/lab/memo.txt", "data/lab/quote-b.txt"),
        fact="lab-total",
    ),
)

# 题面统一加输出格式要求 —— **一处定义，24 条都受约束**。
#
# 为什么用 `replace` 批量加，而不是手改 24 个字符串：手改**必然漏一条**，
# 而漏掉的那条正好会变成"题面没要求结论行、断言却要求" —— 就是 T13 那个 bug
# 的翻版（题面与断言互相矛盾）。"必须对全部 24 条同时成立"的约束，
# 就该由代码保证，不该由记性保证。
TASKS = tuple(
    replace(task, prompt=task.prompt + _FORMAT_SUFFIX) for task in TASKS
)

# 所有任务的**产物**汇总。运行器在**每一次运行之前**都清这一整份，不按任务挑。
#
# 为什么不能按任务挑（PLAN §5 第 27 条）：评测是**交错**跑的（T1..T5 → T1..T5），
# T4 写下的 logs/answers.txt 会留在盘上，下一条 T1 就能读到它 ——
# 而那个文件的内容正是 T1 的答案。清"这条任务的产物"在交错的场景下没有任何意义。
#
# 从 `reset` 派生而不是另写一份：`reset` 表达的是"这条任务会产出这些"，
# 这个事实不该有第二个数据源。
PRODUCTS: tuple[str, ...] = tuple(sorted({rel for task in TASKS for rel in task.reset}))

BY_ID: dict[str, Task] = {t.id: t for t in TASKS}
BY_PROMPT: dict[str, Task] = {t.prompt: t for t in TASKS}

# 题面改过、但历史轨迹还要拿来核对的任务。
LEGACY_PROMPTS: dict[str, str] = {
    # ① 批量派生：**去掉结论行那句之后**的题面，就是"上一次的题面"。
    #
    #    第 4 步收尾给全部 24 条加了输出格式要求，于是**每一条题面都变了**。
    #    照旧一条条手写必然漏，而且漏了不会报错 —— 只会让老轨迹悄悄变成
    #    "题面对不上任何任务"，证据还在、却没法再核对。派生就没有漏的可能。
    task.prompt.removesuffix(_FORMAT_SUFFIX): task.id for task in TASKS
}
# ② 手工补：题面被改过**两次**的那两条，上一版也不是"去掉后缀"能得到的。
LEGACY_PROMPTS.update({
    # T4 的原题面引用了内部编号"T1"，实测让模型烧掉 47288 tokens（PLAN §2 裁定 8）。
    # 那条失败轨迹是**最有价值的证据之一**，不能因为它对应不上现题面就丢掉。
    "把 T1 的答案写入 logs/answers.txt，然后读回来确认写对了。": "T4",
    # T13 的题面在首次真跑后改过（加了"不要列举不符合的文件、不要解释"），
    # 因为断言禁止出现那 3 个文件名，题面就必须也禁止。
    "data/lab/ 目录里哪些文件不含任何阿拉伯数字？把文件名全部列出来。": "T13",
    # T14 的题面跟着 terms.txt 的术语改过（"单笔付款" → "单笔合同金额"）。
    "data/lab/terms.txt 规定单笔付款超过多少元需要组长审批？": "T14",
})


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

# 这些 fixture **必须不存在** —— 有三条任务的前提就是"找不到它"。
# 哪天有人手建了其中一个，任务会悄悄变成另一道题，而通过率看起来仍然正常。
MUST_NOT_EXIST: tuple[str, ...] = (
    "data/handbook-v2.txt",  # T3：如实说找不到
    "data/lab/quote-c.txt",  # T17：读不到就不要写
)


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
        if not task.fact.strip():
            problems.append(f"{task.id} 没写 fact —— 报告无法按「独立事实」归组")

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

    # 全局前置清理会删掉 PRODUCTS 里的每一项 —— 而它是**所有**任务 reset 的并集。
    # 于是"A 的产物恰好是 B 的 fixture"这种坑，只在交错跑时才会炸，
    # 而且表现为"B 偶尔失败"，极难归因。在这里提前拦掉。
    all_fixtures = {rel for task in TASKS for rel in task.fixtures}
    for rel in PRODUCTS:
        if rel in all_fixtures:
            owners = [t.id for t in TASKS if rel in t.fixtures]
            problems.append(f"产物 {rel} 同时是 {owners} 的 fixture —— 全局清理会把它删掉")

    # ---- RESTORE：被任务改写、跑前要还原的 fixture ------------------------
    for rel in RESTORE:
        if not (MINI_ROOT / rel).exists():
            problems.append(f"RESTORE 里的 {rel} 不存在 —— 还原一个不存在的文件是空操作")
        if rel in PRODUCTS:
            problems.append(f"{rel} 同时出现在 PRODUCTS 和 RESTORE 里 —— 删还是还原？")
        if rel not in all_fixtures:
            # 还原一个没有任何任务声明的文件，说明声明漏了
            problems.append(f"RESTORE 里的 {rel} 没有任何任务把它列为 fixture")

    # ---- 必须**不存在**的 fixture ----------------------------------------
    # 它们是任务的前提（"如实说找不到"），哪天被人手建出来，任务就悄悄变成另一道题。
    for rel in MUST_NOT_EXIST:
        if (MINI_ROOT / rel).exists():
            problems.append(f"{rel} 竟然存在 —— 依赖它不存在的任务测的就不是原来那件事了")
        if rel in all_fixtures:
            problems.append(f"{rel} 被写进了 fixtures，但它必须不存在")

    # ---- 断言串之间不许互为子串 ------------------------------------------
    # 判据是 `token in answer`，所以**一个串严格包含另一个串**就意味着断言会误命中：
    # `"760"` 会命中含 `"1760"` 的答案；`"35"` 会命中 `"350"`。
    # 金额类尤其要小心，而人眼扫一遍 24 条任务是扫不出来的。
    #
    # 相等不算问题：两条任务共用同一个必需串（T8/T21 都要 `设备`、T9/T23 都要 `1449`）
    # 是正常的，它们只是在测相邻的两个问题。要拦的是**严格包含**。
    tokens: list[tuple[str, str]] = []  # (任务 id, 串)
    for task in TASKS:
        for attr in ("all_of", "none_of"):
            for token in getattr(task.check, attr, ()):
                tokens.append((f"{task.id}:{attr}", token))
    for owner, token in tokens:
        for other_owner, other in tokens:
            if token == other or token not in other:
                continue
            # 同一个任务里 `none_of` 包含 `all_of` 的串是**故意的** ——
            # 那正是"排除否定说法"的机制（all_of=需要 + none_of=不需要）。
            # 要拦的是别的组合：同一列表内部互相包含，或者跨任务包含。
            owner_task, owner_list = owner.split(":")
            other_task, other_list = other_owner.split(":")
            if owner_task == other_task and owner_list == "all_of" and other_list == "none_of":
                continue
            problems.append(
                f'断言串 "{token}"（{owner}）是 "{other}"（{other_owner}）的子串 '
                "—— `in answer` 会误命中"
            )

    # ---- 每条断言都必须能在**无意义答案**上判失败 ------------------------
    # 这是最便宜的一条"断言太松"探测器：一个连无意义输入都判通过的断言，
    # 跑一万次也只会给你 100% 和零信息（复盘 §6 那类"通过但没测到"）。
    #
    # ⚠️ 探针换过一次（PLAN §5 第 47 条）。原来只喂 `""`，但加了「结论行」契约之后
    # 它**空转**了：`_needs` 第一件事是解析结论行，`""` 里没有结论行 → 直接判失败，
    # 于是 24 条全部"通过"这条探针，token 逻辑一次都没被执行到。
    # 实测 `""` / `" "` / `"结论："` / `"结论：—"` 在 24 条上全部判失败 —— 探针永远绿，
    # 一个 `_needs(())` 那样的空断言它根本抓不到。
    # 能穿过结论行解析、直达 token 逻辑的输入，必须是**有结论行但内容无用**的那一条。
    _LOOSE_PROBES = ("", "结论：不知道")
    for task in TASKS:
        for probe in _LOOSE_PROBES:
            try:
                ok, why = task.check(probe, None)
            except Exception as exc:  # noqa: BLE001 —— 崩了也是问题，要报出来
                problems.append(f"{task.id} 的断言在 {probe!r} 上抛异常：{exc!r}")
                continue
            if ok:
                problems.append(
                    f"{task.id} 的断言在 {probe!r} 上判**通过**（{why}）—— 太松了"
                )

    return problems
