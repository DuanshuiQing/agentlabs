import unittest

from notes.model import Note
from notes.query import filter_notes, search, sort_by_title, tag_index
from notes.store import make_note


def sample() -> list[Note]:
    return [
        make_note("n1", "TODO 清单", body="写周报", tags=["工作", "urgent"]),
        make_note("n2", "会议纪要", body="排期 TODO 已确认", tags=["工作"]),
        make_note("n3", "购书单", body="买两本算法书", tags=["生活"]),
    ]


class TestSearch(unittest.TestCase):
    def test_search_matches_title_and_body(self):
        found = search(sample(), "算法")
        self.assertEqual(["n3"], [n.id for n in found])

    def test_search_empty_keyword_returns_all(self):
        self.assertEqual(3, len(search(sample(), "   ")))

    def test_search_ignores_case(self):
        found = search(sample(), "todo")
        self.assertEqual(["n1", "n2"], [n.id for n in found])


class TestFilter(unittest.TestCase):
    def test_filter_by_tag(self):
        found = filter_notes(sample(), tag="工作")
        self.assertEqual(["n1", "n2"], [n.id for n in found])

    def test_filter_by_tag_and_keyword(self):
        found = filter_notes(sample(), tag="工作", keyword="周报")
        self.assertEqual(["n1"], [n.id for n in found])

    def test_filter_without_conditions(self):
        self.assertEqual(3, len(filter_notes(sample())))


class TestSort(unittest.TestCase):
    def test_sort_by_title_is_case_insensitive(self):
        notes = [make_note("a", "beta"), make_note("b", "Alpha"), make_note("c", "Gamma")]
        self.assertEqual(["b", "a", "c"], [n.id for n in sort_by_title(notes)])


class TestTagIndex(unittest.TestCase):
    def test_tag_index_maps_ids(self):
        index = tag_index(sample())
        self.assertEqual(["n1", "n2"], index["工作"])
        self.assertEqual(["n1"], index["urgent"])

    def test_second_call_does_not_reuse_previous_entries(self):
        first = tag_index([make_note("n1", "一", tags=["x"])])
        second = tag_index([make_note("n2", "二", tags=["y"])])
        self.assertEqual(["x"], list(first))
        self.assertEqual(["y"], list(second))


if __name__ == "__main__":
    unittest.main()
