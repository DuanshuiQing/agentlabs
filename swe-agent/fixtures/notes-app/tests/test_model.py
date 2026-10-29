import unittest

from notes.model import MAX_TITLE, Note, NoteValidationError


class TestNoteValidation(unittest.TestCase):
    def test_empty_id_rejected(self):
        with self.assertRaises(NoteValidationError):
            Note(id="   ", title="合法标题")

    def test_blank_title_rejected(self):
        with self.assertRaises(NoteValidationError):
            Note(id="n1", title="  ")

    def test_too_long_title_rejected(self):
        with self.assertRaises(NoteValidationError):
            Note(id="n1", title="字" * (MAX_TITLE + 1))

    def test_exactly_max_title_ok(self):
        note = Note(id="n1", title="字" * MAX_TITLE)
        self.assertEqual(MAX_TITLE, len(note.title))


class TestNoteFields(unittest.TestCase):
    def test_tags_are_stripped_and_ordered(self):
        note = Note(id="n1", title="标题", tags=["  b ", "", "a  ", "b"])
        self.assertEqual(["b", "a", "b"], note.tags)

    def test_roundtrip_dict(self):
        note = Note(id="n1", title="标题", body="正文", tags=["x"], created_at="2026-10-01")
        again = Note.from_dict(note.to_dict())
        self.assertEqual(note, again)

    def test_from_dict_tolerates_missing_optionals(self):
        note = Note.from_dict({"id": "n1", "title": "只有必填项"})
        self.assertEqual([], note.tags)
        self.assertEqual("", note.body)

    def test_matches_is_case_insensitive(self):
        note = Note(id="n1", title="TODO 清单", body="写周报")
        self.assertTrue(note.matches("todo"))
        self.assertTrue(note.matches("周报"))
        self.assertFalse(note.matches("会议"))

    def test_matches_empty_keyword(self):
        self.assertTrue(Note(id="n1", title="任意").matches(""))


if __name__ == "__main__":
    unittest.main()
