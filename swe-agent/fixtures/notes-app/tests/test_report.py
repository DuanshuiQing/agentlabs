import unittest

from notes.report import average_length, ranked_tags, summarize, summarize_raw
from notes.store import make_note


class TestRankedTags(unittest.TestCase):
    def test_orders_by_count_descending(self):
        notes = [
            make_note("n1", "一", tags=["工作", "urgent"]),
            make_note("n2", "二", tags=["工作"]),
            make_note("n3", "三", tags=["生活"]),
        ]
        self.assertEqual(["工作", "urgent", "生活"], ranked_tags(notes))

    def test_ties_keep_first_seen_order(self):
        notes = [
            make_note("n1", "一", tags=["zzz"]),
            make_note("n2", "二", tags=["aaa"]),
        ]
        self.assertEqual(["zzz", "aaa"], ranked_tags(notes))

    def test_empty(self):
        self.assertEqual([], ranked_tags([]))


class TestSummarize(unittest.TestCase):
    def test_summarize_basic(self):
        notes = [
            make_note("n1", "一", body="12345", tags=["a"]),
            make_note("n2", "二", body="123", tags=["a"]),
        ]
        info = summarize(notes)
        self.assertEqual(2, info["count"])
        self.assertEqual(["a"], info["tags"])
        self.assertEqual(5, info["longest"])


class TestSummarizeRaw(unittest.TestCase):
    def test_missing_tags_key_counts_as_empty(self):
        records = [{"id": "n1", "title": "旧记录"}, {"id": "n2", "title": "新记录", "tags": ["x"]}]
        info = summarize_raw(records)
        self.assertEqual(2, info["count"])
        self.assertEqual(["x"], info["tags"])

    def test_empty_input(self):
        self.assertEqual({"count": 0, "tags": []}, summarize_raw([]))


class TestAverageLength(unittest.TestCase):
    def test_empty_list_is_zero(self):
        self.assertEqual(0.0, average_length([]))

    def test_average(self):
        notes = [make_note("n1", "一", body="1234"), make_note("n2", "二", body="12")]
        self.assertEqual(3.0, average_length(notes))


if __name__ == "__main__":
    unittest.main()
