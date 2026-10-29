"""fixture 自检：把 PLAN §4.4 的三个硬要求变成**可执行的断言**。

为什么必须有这个文件：

    那三条硬要求全是"没做到也不会报错"的类型。干扰项里多写了一个日期、
    金额被复制进第二个文件、`handbook-v2.txt` 不小心被创建 ——
    T2 / T3 依然会"通过"，只是**通过的原因变了**。没有断言的规格等于没有规格。

    数据文件是**会被人手改**的（换几个字、加一行备注都很自然），
    而改坏的后果不会当场暴露，会在几天后表现为"评测结果看不懂"。

规格来源：PLAN §4.4 的数据文件规格表 + §2 口径裁定第 7 条。

运行（纯本地，不发 API 请求）：
    mini-agent\\.venv\\Scripts\\python.exe mini-agent\\check_data.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

# PLAN §4.4：这个金额是 T2 的答案，全库只许出现一次
AMOUNT = "3800"
# PLAN §4.4：T2 的答案源 + 两个干扰项
ANSWER_FILE = "budget-Q4.txt"
DISTRACTORS = ("agenda-1020.txt", "roster.txt")
T1_SOURCE = "handbook.txt"
EXPECTED = (T1_SOURCE, ANSWER_FILE, *DISTRACTORS)
# PLAN §4.4 硬要求 3：这个文件必须**真的不存在**
MUST_NOT_EXIST = "handbook-v2.txt"
# PLAN §4.4 硬要求 1：不许自我暴露
SELF_REVEALING = ("干扰项", "干扰", "测试文件", "示例文件", "样例", "本文件不含", "这是测试")
# PLAN §4.4：handbook.txt 里必须写明日志文件名约定（T1 的答案）
T1_ANSWER_HINT = "run-<时间戳>.jsonl"

# ---------------------------------------------------------------------------
# 阶段 1 第 4 步：data/lab/ 那一组（T6–T24 用）
#
# 设计意图见 evals/tasks.py 里 T6 那一段注释。这里只说断言口径。
# ---------------------------------------------------------------------------
LAB = DATA / "lab"
LAB_EXPECTED = (
    "plan.txt",  # 计划说明：只讲构成，不含金额
    "quote-a.txt",  # 供应商 A 报价
    "quote-b.txt",  # 供应商 B 报价
    "memo.txt",  # 筹备会备忘：决定采用 B —— **结论在这里，数字在别处**
    "terms.txt",  # 付款条款：预付比例、审批线
    "staff.txt",  # 分工名单
    "draft.txt",  # 待填写的草稿（T15/T16 往里写，T17 必须不写）
)
# 这些文件**不许**有阿拉伯数字（T13 的答案就是这一句）
LAB_NO_DIGITS = ("plan.txt", "memo.txt", "staff.txt", "draft.txt")
# 这些文件**必须**有阿拉伯数字
LAB_WITH_DIGITS = ("quote-a.txt", "quote-b.txt", "terms.txt")
# T17 的前提：这个文件必须读不到
LAB_MUST_NOT_EXIST = ("quote-c.txt",)


def read_lab(name: str) -> str:
    return (LAB / name).read_text(encoding="utf-8")


def read(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8")


def digit_runs(text: str) -> list[str]:
    """把文本里所有阿拉伯数字串抓出来（日期、房间号、条号都算）。"""
    return re.findall(r"\d+", text)


def main() -> int:
    problems: list[str] = []
    print(f"数据目录：{DATA}\n")

    # ---- 1. 文件清单必须与 §4.4 规格表一致 -------------------------------
    present = sorted(p.name for p in DATA.iterdir() if p.is_file())
    print(f"[1] data/ 现有文件：{present}")
    for name in EXPECTED:
        if name not in present:
            problems.append(f"缺少规格要求的文件：{name}")
    for name in present:
        if name not in EXPECTED:
            problems.append(f"出现了规格表以外的文件：{name}（模型会把它当候选）")

    # ---- 2. T3 的目标必须真的不存在（硬要求 3）--------------------------
    exists = (DATA / MUST_NOT_EXIST).exists()
    print(f"[2] {MUST_NOT_EXIST} 存在吗：{exists}")
    if exists:
        problems.append(f"{MUST_NOT_EXIST} 竟然存在 —— T3 测的就不是'文件不存在'了")

    # ---- 3. 干扰项不得含任何阿拉伯数字（硬要求 2 + 裁定 7）-------------
    print("[3] 各文件的阿拉伯数字：")
    for name in EXPECTED:
        if name not in present:
            continue
        found = digit_runs(read(name))
        if name in DISTRACTORS:
            print(f"      {name:<18} {found or '（无）'}   ← 干扰项，必须是（无）")
            if found:
                problems.append(f"{name} 含阿拉伯数字 {found} —— 会被当成金额候选")
        elif name == ANSWER_FILE:
            print(f"      {name:<18} {found}   ← 答案源，只许有这一个金额")
        else:
            print(f"      {name:<18} {found}   ← T1 答案源，不在 T2 候选范围，按裁定 7 不受约束")

    # ---- 4. 金额唯一，且只在答案源里（硬要求 2）------------------------
    print(f"[4] 金额 {AMOUNT} 出现在哪：")
    total = 0
    for name in EXPECTED:
        if name not in present:
            continue
        count = read(name).count(AMOUNT)
        total += count
        if count:
            print(f"      {name} 出现 {count} 次")
    if total != 1:
        problems.append(f"金额 {AMOUNT} 全库出现 {total} 次，必须恰好 1 次")
    if AMOUNT not in read(ANSWER_FILE):
        problems.append(f"{ANSWER_FILE} 里没有金额 {AMOUNT}")
    print(f"      合计 {total} 次（必须 = 1）")

    # ---- 5. 干扰项不得自我暴露（硬要求 1）------------------------------
    print("[5] 干扰项自我暴露检查：")
    hit = False
    for name in DISTRACTORS:
        if name not in present:
            continue
        text = read(name)
        for word in SELF_REVEALING:
            if word in text:
                hit = True
                problems.append(f"{name} 里出现了自我暴露字样「{word}」")
    print("      未发现自我暴露字样" if not hit else "      见下方问题列表")

    # ---- 6. T1 的答案源必须真的写着答案 -------------------------------
    ok = T1_ANSWER_HINT in read(T1_SOURCE) if T1_SOURCE in present else False
    print(f"[6] {T1_SOURCE} 含『{T1_ANSWER_HINT}』：{ok}")
    if not ok:
        problems.append(f"{T1_SOURCE} 里找不到 T1 的答案文本")

    # ===================================================================
    # 7–10：阶段 1 第 4 步新增的 data/lab/ 那一组（T6–T24 用）
    #
    # 为什么每组 fixture 都要有自己的断言：这一组的设计意图全是
    # "**没做到也不会报错**"的类型 —— 哪个文件漏了一个金额、两个金额变成子串、
    # 某个"本该没有数字"的文件被人顺手改了一个数字，任务都还会跑，
    # 只是测的已经不是原来那件事了。
    # ===================================================================
    print("\n[7] data/lab/ 文件清单（T6–T24 的 fixture）：")
    lab_present = sorted(p.name for p in LAB.iterdir() if p.is_file())
    for name in LAB_EXPECTED:
        if name not in lab_present:
            problems.append(f"data/lab/ 缺少 {name}")
    for name in lab_present:
        if name not in LAB_EXPECTED:
            problems.append(f"data/lab/ 出现了计划外的文件：{name}（模型会把它当候选）")
    print(f"      {lab_present}")
    for name in LAB_MUST_NOT_EXIST:
        exists = (LAB / name).exists()
        print(f"      {name} 存在吗：{exists}   ← 必须 False")
        if exists:
            problems.append(f"data/lab/{name} 竟然存在 —— 依赖它不存在的任务就废了")

    print("[8] 数字分布（哪些文件该有、哪些该没有）：")
    lab_numbers: dict[str, list[str]] = {}
    for name in LAB_EXPECTED:
        if name not in lab_present:
            continue
        found = digit_runs(read_lab(name))
        lab_numbers[name] = found
        if name in LAB_NO_DIGITS:
            print(f"      {name:<14} {found or '（无）'}   ← 必须（无）")
            if found:
                problems.append(f"data/lab/{name} 含阿拉伯数字 {found} —— T13 的答案就变了")
        elif name in LAB_WITH_DIGITS:
            print(f"      {name:<14} {found}   ← 必须有数字")
            if not found:
                problems.append(f"data/lab/{name} 里一个数字都没有 —— T13 的答案就变了")

    # 断言判据是 `token in answer`，所以**一个金额是另一个的子串**就会误命中：
    # 判 `"500"` 时，答案是 `"1540"` 也会通过。这里自动把 lab 里的数字全抽出来查，
    # 不硬编码清单 —— 改了 fixture 也不会漏检。
    print("[9] data/lab/ 里的数字两两不许互为子串（`in answer` 的正确性前提）：")
    flat = sorted({n for nums in lab_numbers.values() for n in nums})
    collisions = [(a, b) for a in flat for b in flat if a != b and a in b]
    print(f"      全部数字：{flat}")
    if collisions:
        print(f"      见下方问题列表（{len(collisions)} 组冲突）")
        for a, b in collisions:
            problems.append(f'data/lab/ 里 "{a}" 是 "{b}" 的子串 —— 子串断言会误命中')
    else:
        print("      无冲突")

    # 新 fixture 同样不许自我暴露（PLAN §4.4 硬要求 1）
    print("[10] data/lab/ 自我暴露检查：")
    hit = False
    for name in LAB_EXPECTED:
        if name not in lab_present:
            continue
        text = read_lab(name)
        for word in SELF_REVEALING:
            if word in text:
                hit = True
                problems.append(f"data/lab/{name} 里出现了自我暴露字样「{word}」")
    print("      未发现自我暴露字样" if not hit else "      见下方问题列表")

    # ---- 结论 ----------------------------------------------------------
    print()
    if problems:
        print(f"失败：{len(problems)} 个问题")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print(f"全部通过：data/ 的 {len(EXPECTED)} 个文件符合 PLAN §4.4 规格，"
          f"data/lab/ 的 {len(LAB_EXPECTED)} 个文件符合第 4 步的规格。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
