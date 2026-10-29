"""查询：搜索、过滤、排序、标签索引。全部是纯函数，便于测试。"""

from __future__ import annotations

from notes.model import Note


def search(notes: list[Note], keyword: str) -> list[Note]:
    """标题或正文包含关键词（大小写不敏感）。空关键词返回全部。

    大小写不敏感是**两边**都要 lower() —— 只把关键词转小写，
    `TODO` 就永远搜不到 `todo`（见 tests/test_query.py 的用例）。
    """
    needle = (keyword or "").strip().lower()
    if not needle:
        return list(notes)
    return [n for n in notes if needle in n.title or needle in n.body]


def filter_notes(
    notes: list[Note], tag: str | None = None, keyword: str | None = None
) -> list[Note]:
    """先按标签过滤，再按关键词过滤；两个条件都可省略。"""
    picked = list(notes)
    if tag:
        picked = [n for n in picked if tag in n.tags]
    if keyword:
        picked = search(picked, keyword)
    return picked


def sort_by_title(notes: list[Note]) -> list[Note]:
    return sorted(notes, key=lambda n: n.title.lower())


def tag_index(notes: list[Note], index: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """标签 → 笔记 id 列表。

    `index` 允许调用方传入一个已有的索引继续追加；默认 `None` 而不是 `{}`，
    因为可变默认参数会在多次调用之间共享 —— 第二次调用就会带上第一次的条目。
    """
    if index is None:
        index = {}
    for note in notes:
        for tag in note.tags:
            index.setdefault(tag, []).append(note.id)
    return index
