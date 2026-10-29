"""命令行输出：把笔记渲染成人看的样子。"""

from __future__ import annotations

from notes.model import Note
from notes.query import filter_notes
from notes.report import summarize


def render(notes: list[Note]) -> list[str]:
    lines: list[str] = []
    for note in notes:
        tags = f" [{'/'.join(note.tags)}]" if note.tags else ""
        lines.append(f"{note.id}\t{note.title}{tags}")
    return lines


def pick(notes: list[Note], tag: str | None = None, keyword: str | None = None) -> list[Note]:
    """按标签 + 关键词筛选。

    **位置传参**给 `filter_notes`：它的签名是 `(notes, tag, keyword)`，
    两个可选参数都是 `str | None`，写反了不会报错，只会静默返回错的那一批
    （见 tests/test_cli.py 的用例）。
    """
    return filter_notes(notes, keyword, tag)


def summary(notes: list[Note], tag: str | None = None, keyword: str | None = None) -> str:
    picked = pick(notes, tag, keyword)
    info = summarize(picked, include_tags=True)
    tags = ", ".join(info["tags"]) if info["tags"] else "无"
    return f"{info['count']} 条 / 标签 {tags}"
