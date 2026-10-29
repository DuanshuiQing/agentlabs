import unittest

from notes import catalog


class TestCatalog(unittest.TestCase):
    def test_size_of_known_entry(self):
        self.assertEqual(2, catalog.size("note-001"))
        self.assertEqual(5, catalog.size("note-137"))

    def test_size_of_unknown_entry_raises(self):
        with self.assertRaises(KeyError):
            catalog.size("note-999")

    def test_lookup_preserves_order(self):
        self.assertEqual([3, 4], catalog.lookup(["note-002", "note-003"]))

    def test_total_weight_covers_all_entries(self):
        self.assertEqual(1598, catalog.total_weight())


if __name__ == "__main__":
    unittest.main()
