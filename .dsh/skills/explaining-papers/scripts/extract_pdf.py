"""把一个 PDF 抽成「带页码标记的纯文本」—— 笔记流程的第 1 步。

## 为什么这一步必须是脚本

`explaining-papers` 的整个"可核对"建立在**页码锚点**上：每个数字后面那个 `[p.N]`。
而页码只有在抽取时就打进去才准 —— 事后回 PDF 找是找不回来的。
这一步又是纯机械的：一次抽取、一次统计、格式固定。**有唯一正确答案 → 写成脚本。**

## 为什么它以前不在这里（这条也是检验查出来的）

原来的 SKILL.md 第 1 步只写"先把 PDF 抽成带页码的文本"，没给脚本，
依赖的是 `.tmp/pdflib` 里手工解包的 pypdf —— 而 `.tmp/` 是 gitignore 的。
于是这个 skill **换台机器第一步就走不下去**，而且不会有任何报错，只会让 agent 自己临时想办法。

## 依赖与退路

需要 `pypdf`。三档：

    # 1) 装在当前解释器里（最好）
    python -m pip install pypdf

    # 2) 没有 pip（本项目沙箱就是这样）：把 wheel 解包到某个目录，用 --lib 指过去
    #    .tmp/pdflib 里已经有 pypdf 6.21.2，可以直接用
    python extract_pdf.py x.pdf --lib .tmp/pdflib

    # 3) 都拿不到 —— 脚本会**明确报错并告诉你去哪拿**，而不是静默产出一份空文本

## 用法

    python extract_pdf.py paper/2406.13352v3.pdf --out .tmp/text/2406.13352v3.txt
    python extract_pdf.py x.pdf --pages 1-3          # 只抽前 3 页（先看一眼）
    python extract_pdf.py x.pdf --lib .tmp/pdflib

输出：`===== PAGE n =====` + 每页正文；结尾统计页数、字符数、**疑似乱码页**（含替换符 U+FFFD）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

MARK = "===== PAGE {n} ====="


def load_reader(lib: str | None):
    """拿到 pypdf.PdfReader。找不到就返回 (None, 说明)。"""
    try:
        from pypdf import PdfReader

        return PdfReader, "已安装的 pypdf"
    except ImportError:
        pass
    if lib:
        sys.path.insert(0, str(Path(lib).resolve()))
        try:
            from pypdf import PdfReader

            return PdfReader, f"从 {lib} 加载的 pypdf"
        except ImportError as exc:
            return None, f"--lib {lib} 里也没有 pypdf（{exc}）"
    return None, "没有 pypdf"


def parse_pages(spec: str | None, total: int) -> list[int]:
    """`1-3,7` -> 页码列表（1 基）。None = 全部。"""
    if not spec:
        return list(range(1, total + 1))
    picked: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            picked.extend(range(int(a), int(b) + 1))
        elif part:
            picked.append(int(part))
    return [p for p in picked if 1 <= p <= total]


def main() -> int:
    parser = argparse.ArgumentParser(description="PDF -> 带页码标记的纯文本")
    parser.add_argument("pdf", help="PDF 路径")
    parser.add_argument("--out", help="写入这个文件（默认打到 stdout）")
    parser.add_argument("--pages", help="只抽这些页，如 1-3,7")
    parser.add_argument("--lib", help="pypdf 所在的目录（没装 pypdf 时用）")
    args = parser.parse_args()

    src = Path(args.pdf)
    if not src.exists():
        print(f"✗ 找不到 {src}")
        return 1

    reader, how = load_reader(args.lib)
    if reader is None:
        print(f"✗ {how}。三档退路：")
        print("  ① python -m pip install pypdf")
        print("  ② 把 pypdf 的 wheel 解包到某目录，再用 --lib 指过去（本项目 .tmp/pdflib 里已有 6.21.2）")
        print("  ③ 都没有：不要硬着头皮往下走，先解决这个依赖 —— 没有页码锚点，后面所有数字都不可核对")
        return 2

    doc = reader(str(src))
    total = len(doc.pages)
    wanted = parse_pages(args.pages, total)
    print(f"来源：{src}（{total} 页）  读取器：{how}  抽取：{wanted[0]}–{wanted[-1]}")

    chunks: list[str] = []
    garbled: list[int] = []
    for n in wanted:
        text = doc.pages[n - 1].extract_text() or ""
        if "\ufffd" in text:
            garbled.append(n)
        chunks.append(f"{MARK.format(n=n)}\n{text.rstrip()}\n")
    body = "\n".join(chunks)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n"：Windows 上 write_text 默认会把 \n 翻成 \r\n（README 第 13 条）
        out.write_text(body, encoding="utf-8", newline="\n")
        print(f"已写入：{out}（{len(body)} 字符）")
    else:
        print(body)

    print(f"统计：{len(wanted)} 页 / {len(body)} 字符 / 疑似乱码页 {garbled or '无'}")
    if garbled:
        print("  ⚠️ 这些页含替换符 U+FFFD —— 引用它们的内容前先回 PDF 核对")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
