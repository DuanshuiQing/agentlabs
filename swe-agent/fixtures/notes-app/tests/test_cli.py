import unittest

from notes.cli import render, summary
from notes.store import make_note


def sample():
    return [
        make_note("n1", "TODO 清单", body="写周报", tags=["工作", "urgent"]),
        make_note("n2", "会议纪要", body="排期已确认", tags=["工作"]),
        make_note("n3", "购书单", body="买两本算法书", tags=["生活"]),
    ]


class TestRender(unittest.TestCase):
    def test_renders_id_title_and_tags(self):
        lines = render(sample())
        self.assertEqual(3, len(lines))
        self.assertEqual("n1\tTODO 清单 [工作/urgent]", lines[0])

    def test_renders_note_without_tags(self):
        self.assertEqual("n9\t无标签", render([make_note("n9", "无标签")])[0])


class TestSummary(unittest.TestCase):
    def test_summary_without_filters(self):
        self.assertEqual("3 条 / 标签 工作, urgent, 生活", summary(sample()))

    def test_summary_filters_by_tag_then_keyword(self):
        # 先标签、后关键词：只有 n1 同时满足。
        # 不断言标签的**次序**：同计数标签的排序规则由 test_report 单独管，
        # 这里混进来就会让两件事的判据互相牵连（自检器抓到过一次）。
        line = summary(sample(), tag="工作", keyword="周报")
        self.assertTrue(line.startswith("1 条 / 标签"), line)
        self.assertIn("工作", line)


if __name__ == "__main__":
    unittest.main()
