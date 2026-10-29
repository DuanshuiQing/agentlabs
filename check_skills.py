"""检查 `.dsh/skills/` 里的 skill 是否符合发现机制与写作规范。

## 为什么 skill 也需要一个检查脚本

skill 写坏了**不会报错**，只会：
  · 悄悄不被发现（名不是 kebab-case / 目录多了一层 / 根目录放了散装 `.md`）；
  · 被发现了但**永不触发**（description 写得含糊 —— 这是最常见的失败）；
  · 被触发了但**指向一个不存在的脚本**（最阴的一种：模型照着 SKILL.md 敲命令，
    得到的是一句 `No such file`，然后它得自己想办法 —— 而我们看不到这一幕）。

规则来源：[`README.md`](../README.md) 的「skill 的全部规则」
（那几条来自 `dsh-skill-filesystem` 源码，不是猜的）+ `PLAN.md` §2.3 的写作规范。
"什么算一个 skill"这件事只有一个数据源 —— 就是发现机制本身，这个脚本只做它的可执行副本。

## 用法

    mini-agent\\.venv\\Scripts\\python.exe check_skills.py
    mini-agent\\.venv\\Scripts\\python.exe check_skills.py --json
    mini-agent\\.venv\\Scripts\\python.exe check_skills.py --root .tmp\\skill-check   # 换根（自检用）
    mini-agent\\.venv\\Scripts\\python.exe check_skills.py --self-test               # 先证明它会报错

**`--self-test` 才是这个脚本的存在意义**：一个只会在好东西上点头的检查器，
跟没有检查器是一样的（复盘 §6）。它造 7 个坏 skill，逐个确认对应的检查真的报了。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
SKILLS = ROOT / ".dsh" / "skills"

# 内置 skill：项目级 rank 100 里出现同名，会**静默覆盖**一个用户正在依赖的东西
BUILTIN = ("office-docx", "office-pptx", "office-xlsx", "diagnose-windows-sandbox-acl")

KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
# 「何时用」的信号词：description 里必须至少有一个，否则它多半只在描述"做什么"
WHEN = ("当", "时使用", "用于", "如果", "需要", "Use when", "use when", "before", "when you", "when a")
# SKILL.md 里出现的可移植相对路径（scripts/… references/… assets/…）
RELPATH = re.compile(r"(?<![\w/])(scripts|references|assets)/[A-Za-z0-9_./-]+")
ABS = re.compile(r"(?:^|[\s(`\"'])(?:[A-Za-z]:[\\/]|/(?:Users|home)/)")


def parse_frontmatter(text: str) -> dict[str, str]:
    """取 `---` 之间的 `key: value`。只支持单行值（够用，且格式一眼可查）。"""
    found = FRONT.match(text)
    if not found:
        return {}
    data: dict[str, str] = {}
    for line in found.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def body_of(text: str) -> str:
    found = FRONT.match(text)
    return text[found.end():] if found else text


def check_one(path: Path, name: str) -> list[str]:
    """检查一个 SKILL.md。返回问题列表。"""
    problems: list[str] = []
    text = path.read_text(encoding="utf-8-sig")
    rel = path.relative_to(ROOT).as_posix()

    front = parse_frontmatter(text)
    if not front:
        return [f"{rel}：没有 frontmatter（`---` 包裹的 name/description）—— 发现机制会跳过它"]
    for key in ("name", "description"):
        if not front.get(key):
            problems.append(f"{rel}：frontmatter 缺 `{key}`")
    declared = front.get("name", "")
    if declared and not KEBAB.match(declared):
        problems.append(f"{rel}：name `{declared}` 不是 kebab-case（小写字母/数字/连字符）")
    if declared and declared != name:
        problems.append(
            f"{rel}：frontmatter 的 name `{declared}` 与入口名 `{name}` 不一致 —— "
            "catalog 里叫一个名字、本地目录叫另一个，事后对不上号"
        )
    if declared in BUILTIN:
        problems.append(f"{rel}：name `{declared}` 与内置 skill 重名 —— 会静默覆盖它")

    desc = front.get("description", "")
    if desc:
        if len(desc) < 60:
            problems.append(
                f"{rel}：description 只有 {len(desc)} 字符 —— 太短，模型没有足够信息判断何时该用它"
            )
        if not any(word in desc for word in WHEN):
            problems.append(
                f"{rel}：description 里没有「何时用」的线索（当…时 / 用于 / Use when）—— "
                "它可能在描述『做什么』，而触发靠的是『什么时候用』"
            )

    body = body_of(text)
    lines = body.count("\n") + (1 if body.strip() else 0)
    if lines > 500:
        problems.append(f"{rel}：正文 {lines} 行 > 500 —— 拆到 references/（PLAN §2.3）")

    # 引用的相对路径必须真的存在：写错一个字符就是"模型照着敲，得到 No such file"
    for match in RELPATH.finditer(body):
        ref = match.group(0).rstrip(".,;)`")
        target = path.parent / ref
        if not target.exists():
            problems.append(f"{rel}：引用的 `{ref}` 不存在（模型会照着敲，然后拿到 No such file）")

    # 只保留一层深度：references/sub/x.md 这种要读三次才到
    for match in RELPATH.finditer(body):
        if match.group(0).count("/") > 1:
            problems.append(f"{rel}：引用 `{match.group(0)}` 超过一层深度（PLAN §2.3）")

    # 绝对路径不可移植：换台机器就失效，而且会泄露本机目录结构
    for lineno, line in enumerate(body.splitlines(), 1):
        if ABS.search(line):
            problems.append(f"{rel}：正文第 {lineno} 行出现绝对路径 —— 换台机器就失效")
    return problems


def check_root(root: Path) -> list[str]:
    """检查整个 skills 根目录。"""
    problems: list[str] = []
    if not root.is_dir():
        return [f"{root} 不存在或不是目录"]
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            continue  # .gitkeep 这类不是 skill
        if entry.is_dir():
            manifest = entry / "SKILL.md"
            if not manifest.exists():
                problems.append(
                    f"{entry.name}/：目录里没有 SKILL.md —— 发现机制只认这一层，不会往里挖"
                )
                continue
            for nested in entry.rglob("SKILL.md"):
                if nested != manifest:
                    problems.append(
                        f"{nested.relative_to(root).as_posix()}：嵌套的 SKILL.md 不会被发现"
                        "（只扫一层，这是新人最常踩的坑）"
                    )
            problems.extend(check_one(manifest, entry.name))
        elif entry.suffix == ".md":
            # 平铺 skill：名字就是文件名
            problems.extend(check_one(entry, entry.stem))
        else:
            problems.append(f"{entry.name}：既不是 skill 目录也不是 .md —— 它不会被当成 skill")
    return problems


# ---------------------------------------------------------------------------
# 自检：造坏样本，确认每个检查真的会报
# ---------------------------------------------------------------------------
BROKEN: dict[str, str] = {
    # 目录名 -> SKILL.md 内容；期望在输出里看到的关键字写在 _EXPECT 里
    "no-frontmatter": "# 只是个 md 文件\n\n没有 frontmatter。\n",
    "bad-name": "---\nname: Bad_Name\ndescription: " + "当需要做某个任务时使用。" * 4 + "\n---\n\n正文\n",
    "short-desc": "---\nname: short-desc\ndescription: 太短了。\n---\n\n正文\n",
    "no-when": "---\nname: no-when\ndescription: " + "这个 skill 提供若干条关于代码风格的建议和检查。" * 2 + "\n---\n\n正文\n",
    "missing-script": "---\nname: missing-script\ndescription: 当需要跑某个脚本时使用，说明足够长足够长足够长足够长。\n---\n\n跑 `scripts/does_not_exist.py`。\n",
    "name-mismatch": "---\nname: other-name\ndescription: 当需要做某件事时使用，这段描述足够长足够长足够长足够长。\n---\n\n正文\n",
    "abs-path": "---\nname: abs-path\ndescription: 当需要读某个目录时使用，这段描述足够长足够长足够长足够长。\n---\n\n读 `D:\\Agent_test\\README.md`。\n",
}
EXPECT = {
    "no-frontmatter": "没有 frontmatter",
    "bad-name": "不是 kebab-case",
    "short-desc": "太短",
    "no-when": "何时用",
    "missing-script": "No such file",
    "name-mismatch": "不一致",
    "abs-path": "绝对路径",
}


def self_test() -> int:
    """在 .tmp 下造 7 个坏 skill + 1 个好 skill，确认前 7 个各自被报、第 8 个不被报。"""
    sandbox = ROOT / ".tmp" / "skill-check"
    if sandbox.exists():
        for p in sorted(sandbox.rglob("*"), reverse=True):
            p.rmdir() if p.is_dir() else p.unlink()
        sandbox.rmdir()
    sandbox.mkdir(parents=True)
    try:
        for name, text in BROKEN.items():
            d = sandbox / name
            d.mkdir()
            (d / "SKILL.md").write_text(text, encoding="utf-8", newline="\n")
        # 平铺形态的坏样本：根目录散装 .md，缺 frontmatter
        (sandbox / "flat-bad.md").write_text("没有 frontmatter 的平铺 md\n", encoding="utf-8", newline="\n")
        # 目录里没有 SKILL.md
        (sandbox / "empty-dir").mkdir()
        # 根目录里的非 .md、非目录条目：既不是目录包也不是平铺 skill
        (sandbox / "stray.txt").write_text("not a skill\n", encoding="utf-8", newline="\n")
        # 一个好样本（带一个真实存在的脚本引用）
        good = sandbox / "good-skill"
        (good / "scripts").mkdir(parents=True)
        (good / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8", newline="\n")
        (good / "SKILL.md").write_text(
            "---\nname: good-skill\n"
            "description: 当需要演示一个合法 skill 时使用；这段描述足够长，含触发线索，也有脚本引用。\n"
            "---\n\n跑 `scripts/run.py`。\n",
            encoding="utf-8",
            newline="\n",
        )

        problems = check_root(sandbox)
        joined = "\n".join(problems)
        failed = 0
        print("=== 自检：每个坏样本都必须被报出来 ===")
        for name, needle in EXPECT.items():
            hit = needle in joined
            print(f"  {'✓' if hit else '✗'} {name:<16} 期望含「{needle}」")
            failed += 0 if hit else 1
        # 好样本不许被误报
        good_problems = [p for p in problems if p.startswith("good-skill/")]
        print(f"  {'✓' if not good_problems else '✗'} good-skill       不许被误报")
        failed += 0 if not good_problems else 1
        # 结构性坏样本
        for needle in ("没有 SKILL.md", "不会被当成 skill"):
            hit = needle in joined
            print(f"  {'✓' if hit else '✗'} 结构             期望含「{needle}」")
            failed += 0 if hit else 1

        print(f"\n共 {len(problems)} 条问题；自检失败项 {failed}")
        return 1 if failed else 0
    finally:
        for p in sorted(sandbox.rglob("*"), reverse=True):
            p.rmdir() if p.is_dir() else p.unlink()
        sandbox.rmdir()
        # 只删自己造的那个目录 —— `.tmp` 里还有别的东西，不能顺手清空


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 .dsh/skills/ 是否符合发现机制与写作规范")
    parser.add_argument("--root", default=str(SKILLS), help="skills 根目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--self-test", action="store_true", help="造坏样本，证明检查真的会报")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    root = Path(args.root).resolve()
    problems = check_root(root)
    if args.json:
        print(json.dumps({"root": str(root), "problems": problems}, ensure_ascii=False, indent=2))
        return 0 if not problems else 1

    skills = [p for p in root.iterdir() if p.name.startswith(".") is False] if root.is_dir() else []
    print(f"根目录：{root}（{len(skills)} 个条目）")
    if problems:
        print(f"\n{len(problems)} 个问题：")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("全部通过：形态、frontmatter、description 触发线索、引用路径、深度、可移植性。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
