"""笔记的数据模型、校验与错误类型。

这一层刻意很薄：只做"一条笔记该长什么样"，不碰存储与查询。
错误类型全部继承自标准库类型的对应分支，于是调用方既能精确捕获
（`except NoteNotFound`），也能用老式的 `except KeyError` 兜底。
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_TITLE = 120


class NoteError(Exception):
    """所有笔记相关错误的基类 —— 便于调用方一把捞。"""


class NoteValidationError(NoteError, ValueError):
    """字段不合法。"""


class NoteNotFound(NoteError, KeyError):
    """按 id 找不到笔记。"""


class DuplicateNote(NoteError, ValueError):
    """同一个 id 被创建了两次。"""


@dataclass
class Note:
    id: str
    title: str
    body: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self) -> None:
        self.id = (self.id or "").strip()
        self.title = (self.title or "").strip()
        self.body = self.body or ""
        # 去掉空白标签、保留出现顺序 —— 顺序在 report.ranked_tags 里要用到
        self.tags = [t.strip() for t in (self.tags or []) if t and t.strip()]
        if not self.id:
            raise NoteValidationError("id 不能为空")
        if not self.title:
            raise NoteValidationError("title 不能为空")
        if len(self.title) > MAX_TITLE:
            raise NoteValidationError(f"title 最多 {MAX_TITLE} 个字符")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "tags": list(self.tags),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Note":
        return cls(
            id=data["id"],
            title=data["title"],
            body=data.get("body", ""),
            tags=data.get("tags") or [],
            created_at=data.get("created_at", ""),
        )

    def matches(self, keyword: str) -> bool:
        """标题或正文包含关键词（大小写不敏感）。空关键词视为全部命中。"""
        needle = (keyword or "").strip().lower()
        if not needle:
            return True
        return needle in self.title.lower() or needle in self.body.lower()
