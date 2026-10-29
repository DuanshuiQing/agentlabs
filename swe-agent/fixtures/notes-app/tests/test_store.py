import shutil
import unittest
from pathlib import Path

from notes.model import DuplicateNote, Note, NoteNotFound
from notes.store import NoteStore, all_tags, make_note

BUILD = Path(__file__).resolve().parents[1] / ".tmp"


class TestNoteStore(unittest.TestCase):
    def setUp(self):
        self.store = NoteStore()
        self.store.add(make_note("n1", "第一条", body="正文一", tags=["工作"]))
        self.store.add(make_note("n2", "第二条", tags=["生活", "工作"]))

    def test_add_and_get(self):
        self.assertIs(self.store.get("n1"), self.store.get("n1"))
        self.assertEqual(2, len(self.store))

    def test_duplicate_id_rejected(self):
        with self.assertRaises(DuplicateNote):
            self.store.add(make_note("n1", "重复的"))

    def test_missing_id_raises_not_found(self):
        with self.assertRaises(NoteNotFound):
            self.store.get("n404")

    def test_remove(self):
        self.store.remove("n1")
        self.assertEqual(1, len(self.store))
        with self.assertRaises(NoteNotFound):
            self.store.get("n1")

    def test_remove_missing_raises_not_found(self):
        with self.assertRaises(NoteNotFound):
            self.store.remove("n404")

    def test_all_tags_dedup_keeps_first_seen_order(self):
        self.assertEqual(["工作", "生活"], all_tags(self.store))


class TestStorePersistence(unittest.TestCase):
    def setUp(self):
        self.dir = BUILD / "store"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self.path = self.dir / "notes.json"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_dump_load_chinese_roundtrip(self):
        store = NoteStore()
        store.add(
            Note(
                id="n1",
                title="周会记录",
                body="讨论了排期",
                tags=["会议"],
                created_at="2026-10-01",
            )
        )
        store.add(
            Note(
                id="n2",
                title="购物清单",
                body="牛奶、鸡蛋",
                tags=["生活"],
                created_at="2026-10-02",
            )
        )

        written = store.dump_json(self.path)
        self.assertEqual(2, written)

        loaded = NoteStore.load_json(self.path)
        self.assertEqual([n.to_dict() for n in store.all()], [n.to_dict() for n in loaded.all()])
        self.assertEqual(["会议"], loaded.get("n1").tags)

    def test_load_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            NoteStore.load_json(self.dir / "不存在.json")


class TestMakeNote(unittest.TestCase):
    def test_make_note_defaults(self):
        note = make_note("n9", "无标签")
        self.assertEqual([], note.tags)
        self.assertEqual("", note.body)

    def test_make_note_with_tags(self):
        note = make_note("n9", "有标签", tags=["a", "b"])
        self.assertEqual(["a", "b"], note.tags)
        self.assertEqual("Note", type(note).__name__)


if __name__ == "__main__":
    unittest.main()
