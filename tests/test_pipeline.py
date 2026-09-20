"""多阶段流水线核心逻辑测试（不依赖第三方包，可独立运行）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.masking import (  # noqa: E402
    RuleInput,
    TextTooLongError,
    mask_pipeline,
    mask_text,
)


def rule(i, pattern, priority=0, replacement="***", rule_id=None):
    return RuleInput(index=i, rule_id=rule_id, pattern=pattern,
                     priority=priority, replacement=replacement)


class TestPipelineBasic(unittest.TestCase):
    def test_stages_chain_sequentially(self):
        r = mask_pipeline(
            "手机 13800001111",
            [
                [rule(0, r"1\d{10}", replacement="[手机]", rule_id="phone")],
                [rule(0, r"\[手机\]", replacement="<P>", rule_id="token")],
            ],
        )
        self.assertEqual(r.final_text, "手机 <P>")
        self.assertEqual(len(r.stages), 2)
        self.assertEqual(r.stages[0].input_text, "手机 13800001111")
        self.assertEqual(r.stages[0].output_text, "手机 [手机]")
        self.assertEqual(r.stages[1].input_text, "手机 [手机]")
        self.assertEqual(r.stages[1].output_text, "手机 <P>")
        self.assertEqual(r.rules_total, 2)

    def test_empty_stages_returns_original(self):
        r = mask_pipeline("原文", [])
        self.assertEqual(r.final_text, "原文")
        self.assertEqual(r.stages, [])
        self.assertEqual((r.matches_found, r.matches_applied), (0, 0))

    def test_stage_with_empty_rules_is_identity(self):
        r = mask_pipeline("abc", [[], [rule(0, r"b", replacement="X")]])
        self.assertEqual(r.final_text, "aXc")
        self.assertEqual(r.stages[0].output_text, "abc")

    def test_empty_text(self):
        r = mask_pipeline("", [[rule(0, r"a", replacement="X")]])
        self.assertEqual(r.final_text, "")
        self.assertEqual(r.stages[0].matches, [])

    def test_aggregated_stats(self):
        r = mask_pipeline(
            "abc",
            [
                [rule(0, r"abc", priority=1, replacement="X"),
                 rule(1, r"b", priority=0, replacement="Y")],
                [rule(0, r"X", replacement="ZZ")],
            ],
        )
        # 阶段0：2 命中，1 应用 1 否决；阶段1：1 命中并应用
        self.assertEqual(r.rules_total, 3)
        self.assertEqual(r.matches_found, 3)
        self.assertEqual(r.matches_applied, 2)
        self.assertEqual(r.matches_rejected, 1)


class TestProvenanceMapping(unittest.TestCase):
    def test_first_stage_match_maps_to_original(self):
        r = mask_pipeline("aa 13800001111 bb", [
            [rule(0, r"1\d{10}", replacement="[X]")],
        ])
        m = r.stages[0].matches[0]
        self.assertEqual((m.start, m.end), (3, 14))
        self.assertEqual((m.source_start, m.source_end), (3, 14))
        self.assertFalse(m.generated)

    def test_match_on_generated_content_is_generated(self):
        r = mask_pipeline("手机13800001111手机13900002222", [
            [rule(0, r"1\d{10}", replacement="[手机]")],
            [rule(0, r"\[手机\]", replacement="<P>")],
        ])
        s1 = r.stages[1]
        self.assertEqual(s1.output_text, "手机<P>手机<P>")
        for m in s1.matches:
            self.assertTrue(m.generated)
            self.assertIsNone(m.source_start)
            self.assertIsNone(m.source_end)
            self.assertEqual(m.matched_text, "[手机]")

    def test_match_spanning_generated_and_original_returns_envelope(self):
        # 阶段0：'1' -> 'AAA'，文本 xyAAA2zw；阶段1 命中 'AA2'，
        # 其中 'AA' 是新生内容，'2' 是原文位置 3
        r = mask_pipeline("xy12zw", [
            [rule(0, r"1", replacement="AAA")],
            [rule(0, r"AA2", replacement="Q")],
        ])
        m = r.stages[1].matches[0]
        self.assertFalse(m.generated)
        self.assertEqual((m.source_start, m.source_end), (3, 4))
        self.assertEqual(r.final_text, "xyAQzw")

    def test_variable_length_shifts_propagate_to_later_stages(self):
        # 阶段0 变长，阶段1 命中原生存内容，原文区间按真实出处给出
        r = mask_pipeline("a12b", [
            [rule(0, r"12", replacement="XXXX")],
            [rule(0, r"b", replacement="[b]")],
        ])
        m = r.stages[1].matches[0]
        # 阶段1 输入 'aXXXXb'，b 在 [5,6)；原文 b 在 [3,4)
        self.assertEqual((m.start, m.end), (5, 6))
        self.assertEqual((m.source_start, m.source_end), (3, 4))
        self.assertFalse(m.generated)

    def test_deletion_shifts_propagate(self):
        r = mask_pipeline("aXbXc", [
            [rule(0, r"X", replacement="")],
            [rule(0, r"b", replacement="[b]")],
        ])
        m = r.stages[1].matches[0]
        self.assertEqual((m.start, m.end), (1, 2))
        self.assertEqual((m.source_start, m.source_end), (2, 3))
        self.assertEqual(r.final_text, "a[b]c")

    def test_backreference_preserves_original_provenance(self):
        # 模板字面量 '['、']' 是新生内容，\1 回填的 9 位数字保留原文出处
        r = mask_pipeline("电话 13800001111 结束", [
            [rule(0, r"1(\d{9})1", replacement=r"[\1]")],
            [rule(0, r"\d{9}", replacement="NINES")],
        ])
        m1 = r.stages[1].matches[0]
        self.assertFalse(m1.generated)
        # 回填组对应原文 [4,13)
        self.assertEqual((m1.source_start, m1.source_end), (4, 13))
        self.assertEqual(r.final_text, "电话 [NINES] 结束")

    def test_literal_only_replacement_is_fully_generated(self):
        r = mask_pipeline("abc", [
            [rule(0, r"b", replacement="XYZ")],
            [rule(0, r"XYZ", replacement="Q")],
        ])
        self.assertTrue(r.stages[1].matches[0].generated)

    def test_untouched_text_stays_aligned(self):
        # 无替换的阶段后，原生存内容仍 1:1 对齐
        r = mask_pipeline("hello world", [
            [rule(0, r"zzz", replacement="Q")],
            [rule(0, r"world", replacement="W")],
        ])
        m = r.stages[1].matches[0]
        self.assertEqual((m.source_start, m.source_end), (6, 11))

    def test_overlap_rejection_combined_with_length_change_and_deletion(self):
        # 阶段0：变长胜出 [0,3)（'012'->'LONGER'）、删除胜出 'abc'；
        # 单字符数字规则全部被否决，其出处仍能映射回初始原文。
        r = mask_pipeline("0123456789abc", [
            [rule(0, r"012", priority=5, replacement="LONGER"),
             rule(1, r"\d", priority=0, replacement="X"),
             rule(2, r"abc", priority=0, replacement="")],
            [rule(0, r"LONGER", replacement="Q")],
        ])
        self.assertEqual(r.stages[0].output_text, "LONGERXXXXXXX")
        self.assertEqual(r.final_text, "QXXXXXXX")
        # 10 个数字命中：'012' 胜出，覆盖 [0,3)，故数字 0/1/2 三个命中被否决；
        # 数字 3-9 互不重叠，照常应用
        rejected = [m for m in r.stages[0].matches if not m.applied]
        self.assertEqual(len(rejected), 3)
        self.assertEqual([m.matched_text for m in rejected], ["0", "1", "2"])
        # 被否决数字的出处仍是其在初始原文中的位置（变长平移不影响出处）
        self.assertEqual(
            [(m.source_start, m.source_end) for m in rejected],
            [(0, 1), (1, 2), (2, 3)],
        )
        # 阶段1 命中 LONGER：完全新生
        self.assertTrue(r.stages[1].matches[0].generated)
        # 阶段1 命中各 X：替换新生，全部 generated
        for m in r.stages[1].matches[1:]:
            self.assertTrue(m.generated)
            self.assertEqual(m.matched_text, "X")

    def test_rejected_match_on_generated_region_is_generated(self):
        r = mask_pipeline("abc", [
            [rule(0, r"b", replacement="XYZ")],
            [rule(0, r"XY", priority=1, replacement="Q"),
             rule(1, r"XYZ", priority=0, replacement="W")],  # 被否决，且整段新生
        ])
        rejected = [m for m in r.stages[1].matches if not m.applied][0]
        self.assertTrue(rejected.generated)
        self.assertIsNone(rejected.source_start)

    def test_result_spans_remain_stage_local(self):
        r = mask_pipeline("a12b", [
            [rule(0, r"12", replacement="XXXX")],
            [rule(0, r"XXXX", replacement="YY")],
        ])
        m0 = r.stages[0].matches[0]
        self.assertEqual((m0.result_start, m0.result_end), (1, 5))
        m1 = r.stages[1].matches[0]
        self.assertEqual((m1.start, m1.end), (1, 5))
        self.assertEqual((m1.result_start, m1.result_end), (1, 3))

    def test_multiple_disjoint_replacements_backrefs(self):
        r = mask_pipeline("a1 b2", [
            [rule(0, r"([ab])(\d)", replacement=r"<\1:\2>")],
            [rule(0, r"\d", replacement="#")],
        ])
        # 阶段1 命中回填内容中的数字（原文 1、3 位置）
        s1 = r.stages[1]
        srcs = sorted((m.source_start, m.source_end) for m in s1.matches)
        self.assertEqual(srcs, [(1, 2), (4, 5)])
        for m in s1.matches:
            self.assertFalse(m.generated)
        self.assertEqual(r.final_text, "<a:#> <b:#>")

    def test_pipeline_single_stage_matches_mask_text(self):
        text = "abcdefgh"
        rules = [
            rule(0, r"abc", priority=1, replacement="X"),
            rule(1, r"bcd", priority=5, replacement="YY"),
        ]
        one = mask_text(text, rules)
        pipe = mask_pipeline(text, [rules])
        self.assertEqual(pipe.final_text, one.masked_text)
        self.assertEqual(
            [(m.rule_index, m.start, m.end, m.applied) for m in pipe.stages[0].matches],
            [(m.rule_index, m.start, m.end, m.applied) for m in one.matches],
        )


class TestPipelineRuleErrors(unittest.TestCase):
    def test_invalid_rule_reported_only_in_its_stage(self):
        r = mask_pipeline("abc", [
            [rule(0, "(", replacement="X"), rule(1, r"a", replacement="A")],
            [rule(0, r"b", replacement=r"\g<9>")],
            [rule(0, "(", replacement="Y")],
        ])
        self.assertEqual(r.rules_invalid, 3)
        by_stage = {st.index: st.rule_errors for st in r.stages}
        self.assertEqual([(e.rule_index, e.stage_index, e.stage)
                          for e in by_stage[0]], [(0, 0, "compile")])
        self.assertEqual([(e.rule_index, e.stage_index, e.stage)
                          for e in by_stage[1]], [(0, 1, "replacement")])
        self.assertEqual([(e.rule_index, e.stage_index, e.stage)
                          for e in by_stage[2]], [(0, 2, "compile")])
        # 合法规则照常生效，阶段继续推进
        self.assertEqual(r.final_text, "Abc")

    def test_other_stages_unaffected_by_invalid_rule(self):
        r = mask_pipeline("a1b", [
            [rule(0, r"(", replacement="X")],
            [rule(0, r"\d", replacement="[N]")],
        ])
        self.assertEqual(len(r.stages[0].rule_errors), 1)
        self.assertEqual(r.stages[0].matches, [])
        self.assertEqual(r.stages[1].output_text, "a[N]b")
        self.assertEqual(r.stages[1].rule_errors, [])


class TestPipelineLimits(unittest.TestCase):
    def test_initial_text_over_limit(self):
        with self.assertRaises(TextTooLongError) as ctx:
            mask_pipeline("x" * 10, [[rule(0, r"x", replacement="yy")]],
                          max_length=5)
        self.assertEqual(ctx.exception.stage_index, -1)

    def test_intermediate_text_over_limit(self):
        with self.assertRaises(TextTooLongError) as ctx:
            mask_pipeline("xx",
                          [[rule(0, r"x", replacement="yyy")],
                           [rule(0, r"y", replacement="z")]],
                          max_length=5)
        self.assertEqual(ctx.exception.stage_index, 0)
        self.assertEqual(ctx.exception.length, 6)

    def test_shrinking_back_under_limit_is_allowed(self):
        r = mask_pipeline("xx",
                          [[rule(0, r"x", replacement="yyy")],
                           [rule(0, r"yyy", replacement="z")]],
                          max_length=6)
        self.assertEqual(r.final_text, "zz")


if __name__ == "__main__":
    unittest.main()
