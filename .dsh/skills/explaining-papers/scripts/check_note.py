"""校验一份 L1 笔记 / INDEX 是否符合 `explaining-papers` 的格式。

## 为什么要有这个

论文笔记写坏了**不会报错**：少一节、页码全没有、批判只写一句"作者没做大规模实验" ——
它读起来仍然像一篇笔记，但**不能用来核对、也不能拿去讲**。
这个脚本把"一份笔记算不算写完了"变成可执行断言。

规则来自本仓库已经成型的那 4 篇（`notes/papers/`）：节号顺序、`[p.N]` 页码锚点、
五段速记五行、批判的字数与角度、对接要落到代码、术语表只收读前不懂的词。

## 用法

    python .dsh/skills/explaining-papers/scripts/check_note.py notes/papers/agentdojo.md
    python .dsh/skills/explaining-papers/scripts/check_note.py notes/papers/INDEX.md
    python .dsh/skills/explaining-papers/scripts/check_note.py notes/papers/*.md

文件名是 `INDEX.md` 就按 INDEX 规则查，其他按笔记规则查。退出码 1 = 有问题。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 节的"角色" -> 能接受的标题关键词（不同论文的节名不完全一样）
ROLE_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("一句话结论", ("一句话结论", "一句话定性")),
    ("问题", ("问题与动机", "中心论点", "动机", "问题")),
    ("方法", ("方法骨架", "分类学", "方法")),
    ("证据", ("关键数字", "实验设计", "实验", "评测")),
    ("批判", ("批判", "未解问题")),
    ("对接", ("对接",)),
    ("原文句", ("关键原文句", "原文句")),
    ("术语表", ("术语表",)),
]
PAGE = re.compile(r"p\.\d+")
TABLE = re.compile(r"Table\s+[IVXivx\d]+|Fig(?:ure)?\.?\s*\d")
ABS = re.compile(r"(?:^|[\s(`\"'])[A-Za-z]:[\\/]")
HEAD = re.compile(r"^(#{2,3})\s+(.*)$", re.M)


def sections(text: str) -> list[tuple[int, str, int, int]]:
    """[(层级, 标题, 起始 offset, 结束 offset)]。只认 ## 与 ###。"""
    marks = [(len(m.group(1)), m.group(2).strip(), m.start()) for m in HEAD.finditer(text)]
    out = []
    for i, (level, title, start) in enumerate(marks):
        end = len(text)
        for level2, _t2, start2 in marks[i + 1:]:
            if level2 <= level:
                end = start2
                break
        out.append((level, title, start, end))
    return out


def find_role(spans: list[tuple[int, str, int, int]], role: str) -> list[tuple[str, str, int]]:
    """按角色找章节 -> [(标题, 正文, 起始 offset)]。"""
    keys = dict(ROLE_ALIASES)[role]
    hits = []
    for _level, title, start, end in spans:
        if any(k in title for k in keys):
            hits.append((title, "", start))
    return hits


def check_note(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")
    name = path.name
    spans = sections(text)
    body_all = text
    problems: list[str] = []

    # 1) 顶部元信息表
    for row in ("作者", "出处", "原文"):
        if f"| {row}" not in text and f"|{row}" not in text:
            problems.append(f"{name}：顶部元信息表缺「{row}」一行")
    if "阅读范围" not in text:
        problems.append(f"{name}：没写「阅读范围」—— 没读的部分要明说没读")

    # 2) 五段速记五行
    for seg in ("问题", "方法", "实验", "局限", "可复现"):
        if not re.search(rf"\|\s*\*?\*?{seg}", text):
            problems.append(f"{name}：§0 五段速记缺「{seg}」段")

    # 3) 节的顺序
    order = []
    for role, _ in ROLE_ALIASES:
        hits = find_role(spans, role)
        if not hits:
            problems.append(f"{name}：找不到「{role}」这一节（可接受的标题关键词见函数 ROLE_ALIASES）")
            continue
        order.append((spans.index(next(s for s in spans if s[1] == hits[0][0])), role))
    positions = [pos for pos, _ in order]
    if positions != sorted(positions):
        problems.append(f"{name}：节的顺序不对 —— 实际顺序 {[r for _, r in sorted(order)]}")

    # 4) 页码锚点
    anchors = len(PAGE.findall(text))
    if anchors < 20:
        problems.append(f"{name}：只有 {anchors} 个 `[p.N]` 页码锚点（<20）—— 数字没法核对")

    # 5) 表格 / 图引用
    refs = len(TABLE.findall(text))
    if refs < 1:
        problems.append(f"{name}：没有引用任何 Table/Fig —— 方法骨架多半是转述而不是抄表")

    # 6) 批判的字数与角度
    crit = find_role(spans, "批判")
    if crit:
        span = next(s for s in spans if s[1] == crit[0][0])
        crit_text = text[span[2]:span[3]]
        if len(crit_text) < 150:
            problems.append(f"{name}：批判节只有 {len(crit_text)} 字符（<150）—— 只复述局限不算批判")

    # 7) 对接要落到代码
    dock = find_role(spans, "对接")
    if dock:
        span = next(s for s in spans if s[1] == dock[0][0])
        dock_text = text[span[2]:span[3]]
        if not re.search(r"\.py|::|\.dsh/|evals/|mini-agent/", dock_text):
            problems.append(f"{name}：对接节没有落到具体文件/函数 —— 要出现 `.py`、`::` 或目录名")

    # 8) 术语表行数
    gloss = find_role(spans, "术语表")
    if gloss:
        span = next(s for s in spans if s[1] == gloss[0][0])
        rows = [line for line in text[span[2]:span[3]].splitlines() if line.strip().startswith("|")]
        rows = [r for r in rows if not re.fullmatch(r"\|[\s\-:|]+\|", r.strip())]
        # 去掉分隔行之后还剩「表头 + 术语」；要 ≥4 个术语
        if len(rows) < 5:
            problems.append(f"{name}：术语表只有 {len(rows)} 行（含表头）—— 至少 4 个术语")

    # 9) 可移植性
    for lineno, line in enumerate(body_all.splitlines(), 1):
        if ABS.search(line):
            problems.append(f"{name}：第 {lineno} 行出现绝对路径 —— 换台机器就失效")

    return problems


def check_index(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")
    name = path.name
    problems: list[str] = []
    need = {
        "L0 速览卡的「值不值得细读」": "值不值得细读",
        "L0 速览卡的「我的判断」": "我的判断",
        "横向矩阵的「我更信哪部分」": "我更信哪部分",
        "横向矩阵的「主要遗留风险」": "主要遗留风险",
        "三种读法的 10 分钟档": "10 分钟",
        "三种读法的 1 小时档": "1 小时",
        "落地清单的「落在哪」": "落在哪",
        "落地清单的「依据」": "依据",
        "已知缺陷 / 未核实清单": "未核实",
        "更新记录": "更新记录",
    }
    for label, needle in need.items():
        if needle not in text:
            problems.append(f"{name}：缺 {label}（找不到「{needle}」）")
    if not re.search(r"\|[^\n]*\|[^\n]*\|", text):
        problems.append(f"{name}：没有表格 —— INDEX 的 L0 卡与矩阵都是表")
    return problems


def _valid_note() -> str:
    """一个刚刚好满足全部规则的最小笔记。自检的基准。"""
    anchors = " ".join(f"[p.{i}]" for i in range(1, 21))
    critique = "作者没有交代样本量与置信区间，也没有给出任何失败案例。" * 6
    return f"""# Title

| 项 | 内容 |
| --- | --- |
| 作者 | A |
| 出处 | X |
| 原文 | y |
| 阅读范围 | 全文 |

## 0. 五段速记

| 段 | 内容 |
| --- | --- |
| 问题 | a |
| 方法 | b |
| 实验 | c |
| 局限 | d |
| 可复现第一步 | e |

## 1. 一句话结论

一句话结论。

## 2. 问题与动机

1. 动机一

## 3. 方法骨架

### 3.1 构成（Table 1, p.6）

骨架。

## 4. 关键数字

{anchors}

## 5. 结论与作者主张

主张。

## 6. 批判与未解问题

{critique}

## 7. 与 mini-agent 的对接（具体到代码）

改动落在 `tools.py::dispatch`。

## 8. 关键原文句

"a quote" [p.9]

## 9. 术语表

| 术语 | 通用解释 | 谁在用 |
| --- | --- | --- |
| a | b | c |
| d | e | f |
| g | h | i |
| j | k | l |
"""


# (名字, 改坏方式, 期望输出里出现的关键字)
MUTATIONS: list[tuple[str, str, str]] = [
    ("删掉术语表", "replace", "找不到「术语表」"),
    ("去掉页码锚点", "strip", "页码锚点"),
    ("批判压成一句", "shortcrit", "批判节"),
    ("加一条绝对路径", "abs", "绝对路径"),
    ("删掉作者行", "noauthor", "缺「作者」"),
    ("删掉局限段", "nolimit", "缺「局限」"),
    ("对接写成空话", "vaguedock", "对接节没有落到"),
]


def _mutate(base: str, how: str) -> str:
    if how == "replace":
        return base.split("## 9. 术语表")[0]
    if how == "strip":
        return re.sub(r"\[p\.\d+\]", "", base)
    if how == "shortcrit":
        return re.sub(r"## 6\. 批判与未解问题\n\n.*?\n\n## 7", "## 6. 批判与未解问题\n\n太浅。\n\n## 7", base, flags=re.S)
    if how == "abs":
        return base + "\n见 `D:\\Agent_test\\notes`。\n"
    if how == "noauthor":
        return base.replace("| 作者 | A |\n", "")
    if how == "nolimit":
        return base.replace("| 局限 | d |\n", "")
    if how == "vaguedock":
        return re.sub(r"## 7\. .*?\n\n.*?\n\n## 8", "## 7. 与 mini-agent 的对接\n\n可以借鉴其思想。\n\n## 8", base, flags=re.S)
    raise ValueError(how)


def self_test() -> int:
    """一个"只会在好东西上点头"的检查器等于没有。这里逐个证明它真的会报。"""
    root = Path(__file__).resolve().parents[4]
    sandbox = root / ".tmp" / "paper-note-check"
    sandbox.mkdir(parents=True, exist_ok=True)
    ok = 0
    failed = 0
    try:
        base = _valid_note()
        good = sandbox / "good.md"
        good.write_text(base, encoding="utf-8", newline="\n")
        problems = check_note(good)
        print(f"  {'✓' if not problems else '✗'} 合法样本不被误报" + (f"：{problems}" if problems else ""))
        ok += 0 if problems else 1
        failed += 1 if problems else 0

        for label, how, needle in MUTATIONS:
            bad = sandbox / "bad.md"
            bad.write_text(_mutate(base, how), encoding="utf-8", newline="\n")
            joined = "\n".join(check_note(bad))
            hit = needle in joined
            print(f"  {'✓' if hit else '✗'} {label:<14} 期望含「{needle}」")
            ok += 1 if hit else 0
            failed += 0 if hit else 1
    finally:
        for p in sorted(sandbox.glob("*"), reverse=True):
            p.unlink()
        sandbox.rmdir()

    print(f"\n自检 {ok} 项通过 / {failed} 项失败")
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="校验论文笔记 / INDEX 的格式")
    parser.add_argument("paths", nargs="*", help="笔记或 INDEX 的路径")
    parser.add_argument("--self-test", action="store_true", help="造合法样本 + 7 种改坏，确认规则真的会报")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.paths:
        parser.error("要么给路径，要么 --self-test")

    total = 0
    for raw in args.paths:
        path = Path(raw)
        if not path.exists():
            print(f"✗ 找不到 {path}")
            total += 1
            continue
        problems = check_index(path) if path.name.upper() == "INDEX.MD" else check_note(path)
        if problems:
            total += len(problems)
            print(f"\n✗ {path}")
            for item in problems:
                print(f"    {item}")
        else:
            print(f"✓ {path}")
    print(f"\n{'全部通过' if not total else f'共 {total} 个问题'}")
    return 0 if not total else 1


if __name__ == "__main__":
    raise SystemExit(main())
