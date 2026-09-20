"""核心脱敏逻辑测试（不依赖第三方包，可独立运行）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.masking import RuleInput, mask_text  # noqa: E402


def rule(i, pattern, priority=0, replacement="***", rule_id=None):
    return RuleInput(index=i, rule_id=rule_id, pattern=pattern,
                     priority=priority, replacement=replacement)


class TestBasicMasking(unittest.TestCase):
    def test_single_rule_replaces_all_matches(self):
        r = mask_text("电话 13800001111，备用 13900002222",
                      [rule(0, r"1\d{10}", replacement="[手机]")])
        self.assertEqual(r.masked_text, "电话 [手机]，备用 [手机]")
        self.assertEqual(r.applied_count, 2)
        self.assertEqual(r.rejected_count, 0)
        self.assertEqual(r.rule_errors, [])

    def test_empty_rules_returns_original(self):
        r = mask_text("原文不变", [])
        self.assertEqual(r.masked_text, "原文不变")
        self.assertEqual(r.matches, [])

    def test_replacement_template_with_groups(self):
        r = mask_text("邮箱 alice@example.com 待处理",
                      [rule(0, r"(\w+)@(\w+\.\w+)", replacement=r"\1@***")])
        self.assertEqual(r.masked_text, "邮箱 alice@*** 待处理")

    def test_empty_replacement_deletes_match(self):
        r = mask_text("abc123def", [rule(0, r"\d+", replacement="")])
        self.assertEqual(r.masked_text, "abcdef")

    def test_zero_width_matches_are_skipped(self):
        r = mask_text("abc", [rule(0, r"x*", replacement="-")])
        self.assertEqual(r.masked_text, "abc")
        self.assertEqual(r.matches, [])


class TestOverlapResolution(unittest.TestCase):
    def test_higher_priority_wins(self):
        text = "abcdefgh"
        rules = [
            rule(0, r"abc", priority=1, replacement="X"),   # [0,3)
            rule(1, r"bcd", priority=5, replacement="YY"),  # [1,4) 优先级高
        ]
        r = mask_text(text, rules)
        self.assertEqual(r.masked_text, "aYYefgh")
        by_rule = {m.rule_index: m for m in r.matches}
        self.assertEqual(by_rule[1].applied and not by_rule[0].applied, True)
        self.assertEqual(by_rule[0].winner.rule_index, 1)

    def test_same_priority_earlier_start_wins(self):
        text = "abcdefgh"
        rules = [
            rule(0, r"cde", priority=0, replacement="X"),  # [2,5)
            rule(1, r"abc", priority=0, replacement="Y"),  # [0,3) 起始更早
        ]
        r = mask_text(text, rules)
        self.assertEqual(r.masked_text, "Ydefgh")
        by_rule = {m.rule_index: m for m in r.matches}
        self.assertTrue(by_rule[1].applied)
        self.assertFalse(by_rule[0].applied)

    def test_same_start_earlier_rule_order_wins(self):
        text = "aaabbb"
        rules = [
            rule(0, r"aaa", priority=0, replacement="X"),
            rule(1, r"aaab", priority=0, replacement="YYYY"),
        ]
        r = mask_text(text, rules)
        self.assertEqual(r.masked_text, "Xbbb")
        by_rule = {m.rule_index: m for m in r.matches}
        self.assertTrue(by_rule[0].applied)
        self.assertFalse(by_rule[1].applied)

    def test_nested_interval_outer_wins_when_earlier_start(self):
        text = "0123456789"
        rules = [
            rule(0, r"234", priority=0, replacement="X"),     # [2,5)
            rule(1, r"12345", priority=0, replacement="YY"),  # [1,6) 起始更早
        ]
        r = mask_text(text, rules)
        self.assertEqual(r.masked_text, "0YY6789")

    def test_non_overlapping_matches_all_applied(self):
        text = "a1b2c3"
        rules = [rule(0, r"\d", priority=0, replacement="#")]
        r = mask_text(text, rules)
        self.assertEqual(r.masked_text, "a#b#c#")
        self.assertEqual(r.applied_count, 3)

    def test_resolution_is_deterministic(self):
        text = "abcdefgh"
        rules = [
            rule(0, r"abcd", priority=2, replacement="W"),
            rule(1, r"cdef", priority=2, replacement="X"),
            rule(2, r"bc", priority=9, replacement="Y"),
            rule(3, r"[", priority=0, replacement="Z"),  # 非法正则
        ]
        first = mask_text(text, rules)
        for _ in range(5):
            again = mask_text(text, rules)
            self.assertEqual(first.masked_text, again.masked_text)
            self.assertEqual(
                [(m.rule_index, m.start, m.applied) for m in first.matches],
                [(m.rule_index, m.start, m.applied) for m in again.matches],
            )


class TestInvalidRules(unittest.TestCase):
    def test_invalid_regex_reported_and_others_unaffected(self):
        text = "电话 13800001111"
        rules = [
            rule(0, r"(", rule_id="bad"),
            rule(1, r"1\d{10}", replacement="[手机]", rule_id="phone"),
        ]
        r = mask_text(text, rules)
        self.assertEqual(r.masked_text, "电话 [手机]")
        self.assertEqual(len(r.rule_errors), 1)
        err = r.rule_errors[0]
        self.assertEqual((err.rule_index, err.rule_id, err.stage), (0, "bad", "compile"))
        self.assertTrue(err.error)

    def test_invalid_replacement_template_reported(self):
        text = "abc123"
        rules = [
            rule(0, r"\d+", replacement=r"\g<9>"),  # 引用不存在的分组
            rule(1, r"abc", replacement="X"),
        ]
        r = mask_text(text, rules)
        self.assertEqual(r.masked_text, "X123")
        self.assertEqual(len(r.rule_errors), 1)
        self.assertEqual(r.rule_errors[0].stage, "replacement")


class TestSpanMapping(unittest.TestCase):
    def test_result_spans_track_length_changes(self):
        text = "aa 13800001111 bb 13900002222 cc"
        r = mask_text(text, [rule(0, r"1\d{10}", replacement="[X]")])
        self.assertEqual(r.masked_text, "aa [X] bb [X] cc")
        m0, m1 = r.matches
        # 第一个命中：原 [3,14) -> 结果 [3,6)
        self.assertEqual((m0.start, m0.end), (3, 14))
        self.assertEqual((m0.result_start, m0.result_end), (3, 6))
        # 第二个命中：原 [18,29) -> 结果平移 -8 -> [10,13)
        self.assertEqual((m1.start, m1.end), (18, 29))
        self.assertEqual((m1.result_start, m1.result_end), (10, 13))
        # 结果区间内容校验
        self.assertEqual(r.masked_text[3:6], "[X]")
        self.assertEqual(r.masked_text[10:13], "[X]")

    def test_rejected_match_result_span_maps_surviving_fragment(self):
        text = "0123456789"
        rules = [
            rule(0, r"012", priority=5, replacement="LONGER"),  # [0,3) 变长 +3
            rule(1, r"123", priority=0, replacement="X"),       # 被否决
        ]
        r = mask_text(text, rules)
        self.assertEqual(r.masked_text, "LONGER3456789")
        by_rule = {m.rule_index: m for m in r.matches}
        rejected = by_rule[1]
        self.assertFalse(rejected.applied)
        # 原 [1,4) 中仅 "3" 未被胜出区间覆盖，映射到结果 [6,7)
        self.assertEqual((rejected.result_start, rejected.result_end), (6, 7))
        self.assertEqual(r.masked_text[6:7], "3")

    def test_rejected_match_containing_winner_maps_to_whole_landing_region(self):
        text = "0123456789"
        rules = [
            rule(0, r"\d+", priority=0, replacement="X"),   # [0,10) 被否决
            rule(1, r"234", priority=5, replacement=""),    # [2,5) 胜出且删除
        ]
        r = mask_text(text, rules)
        self.assertEqual(r.masked_text, "0156789")
        rejected = {m.rule_index: m for m in r.matches}[0]
        # 原 [0,10) 内容除 [2,5) 外全部存活，结果区间覆盖其落点 [0,7)
        self.assertEqual((rejected.result_start, rejected.result_end), (0, 7))
        self.assertEqual(r.masked_text[0:7], "0156789")


if __name__ == "__main__":
    unittest.main()
