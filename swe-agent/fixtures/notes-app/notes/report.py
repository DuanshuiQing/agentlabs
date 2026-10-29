"""报表：标签排行、摘要、平均长度。"""

from __future__ import annotations

from collections import Counter

from notes.model import Note


def ranked_tags(notes: list[Note]) -> list[str]:
    """标签按出现次数降序；**次数相同时按首次出现的顺序**（稳定，不看字母序）。

    排序键写成 `(-count, first_seen)`：只写 `-count` 的话，Python 的稳定排序
    保持的是 Counter 的插入序，看起来"也对"，但它依赖 Counter 的实现细节；
    显式带上 first_seen 才能把"同计数按首次出现"这条规则钉住。
    """
    counts = Counter(tag for note in notes for tag in note.tags)
    first_seen = {
        tag: order
        for order, tag in enumerate(dict.fromkeys(tag for note in notes for tag in note.tags))
    }
    return sorted(counts, key=lambda tag: (-counts[tag], first_seen[tag]))


def summarize(notes: list[Note], include_tags: bool = True) -> dict:
    """`include_tags=False` 时不计算标签排行（报表里只要条数与最长正文时用）。"""
    return {
        "count": len(notes),
        "tags": ranked_tags(notes) if include_tags else [],
        "longest": max((len(n.body) for n in notes), default=0),
    }


def summarize_raw(records: list[dict]) -> dict:
    """给"从 JSON 原样读出来的 dict"做摘要。

    输入是外部数据，**不能假设字段齐全**：早期导出的记录里没有 `tags` 这个键，
    `rec.get("tags")` 会返回 None，直接 `extend` 就抛
    `TypeError: 'NoneType' object is not iterable`。
    """
    tags: list[str] = []
    for record in records:
        tags.extend(record.get("tags") or [])
    return {"count": len(records), "tags": tags}


def average_length(notes: list[Note]) -> float:
    if not notes:
        return 0.0
    return sum(len(n.body) for n in notes) / len(notes)
