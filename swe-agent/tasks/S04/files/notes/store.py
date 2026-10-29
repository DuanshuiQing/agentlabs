"""笔记的存储：内存字典 + JSON 落盘。没有数据库，没有异步，够用就好。"""

from __future__ import annotations

import json
from pathlib import Path

from notes.model import DuplicateNote, Note, NoteNotFound


class NoteStore:
    def __init__(self) -> None:
        self._notes: dict[str, Note] = {}

    def add(self, note: Note) -> Note:
        if note.id in self._notes:
            raise DuplicateNote(f"id 已存在: {note.id}")
        self._notes[note.id] = note
        return note

    def get(self, note_id: str) -> Note:
        try:
            return self._notes[note_id]
        except KeyError:
            raise NoteNotFound(note_id) from None

    def remove(self, note_id: str) -> None:
        if note_id not in self._notes:
            raise NoteNotFound(note_id)
        del self._notes[note_id]

    def all(self) -> list[Note]:
        return list(self._notes.values())

    def __len__(self) -> int:
        return len(self._notes)

    def dump_json(self, path: str | Path) -> int:
        """按 id 排序后写盘；返回写出的条目数。

        必须显式 `encoding="utf-8"`：Windows 上默认编码是 GBK，
        写得出、读回来却对不上（中文标签会变成乱码或直接解码失败）。
        """
        payload = [n.to_dict() for n in sorted(self._notes.values(), key=lambda n: n.id)]
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            newline="\n",
        )
        return len(payload)

    @classmethod
    def load_json(cls, path: str | Path) -> "NoteStore":
        store = cls()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        for item in raw:
            store.add(Note.from_dict(item))
        return store


def make_note(note_id: str, title: str, body: str = "", tags: list[str] | None = None) -> Note:
    """构造一条笔记的便捷入口。

    `tags` 用 `None` 而不是 `[]` 做默认值：可变默认参数在函数定义时只创建一次，
    所有调用会共享同一个列表，第一次追加就把下一次的结果污染了。
    """
    return Note(id=note_id, title=title, body=body, tags=tags)


def all_tags(store: NoteStore) -> list[str]:
    """store 里出现过的全部标签，去重并按首次出现顺序。"""
    seen: dict[str, None] = {}
    for note in store.all():
        for tag in note.tags:
            seen[tag] = None
    return list(seen)
