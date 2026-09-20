"""多阶段流水线核心逻辑测试（不依赖第三方包，可独立运行）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.masking import RuleInput  # noqa: E402
from app.pipeline import run_pipeline  # noqa: E402


def rule(i, pattern, replacement="***", priority=0, rule_id=None):
    return RuleInput(index=i, rule_id=rule_id, pattern=pattern,
                     priority=priority, replacement=replacement)


class TestPipelineBasic(unittest.TestCase):
    def test_stages_chain_outputs(self):
        r = run_pipeline("abc123def", [
            [rule(0, r"\d+", "#")],       # abc#def
            [rule(0, r"#", "@@")],       # abc@@def
            [rule(0, r"@", "X")],        # abcXXdef
        ])
        self.assertEqual(r.masked_text, "abcXXdef")
        self.assertEqual([st.masked_text for st in r.stages],
                         ["abc#def", "abc@@def", "abcXXdef"])

    def test_later_stage_hits_generated_content_is_null_span(self):
        r = run_pipeline("电话13800001111", [
            [rule(0, r"1\d{10}", "[PHONE]")],
            [rule(0, r"PHONE", "***")],
        ])
        m1 = r.stages[1].matches[0]
        self.assertEqual(m1.matched_text, "PHONE")
        self.assertTrue(m1.generated)
        self.assertIsNone(m1.source_span)
        # 第一阶段命中正常映射回初始原文
        m0 = r.stages[0].matches[0]
        self.assertFalse(m0.generated)
        self.assertEqual(m0.source_span, (2, 13))
        self.assertEqual(m0.input_span, (2, 13))

    def test_group_reference_inherits_source_positions(self):
        r = run_pipeline("alice@example.com", [
            [rule(0, r"(\w+)@(\w+\.\w+)", r"\1::\2")],
            [rule(0, r"example", "EX")],
        ])
        m = r.stages[1].matches[0]
        self.assertFalse(m.generated)
        lo, hi = m.source_span
        self.assertEqual("alice@example.com"[lo:hi], "example")

    def test_first_stage_uses_identity_provenance(self):
        r = run_pipeline("abc", [[rule(0, r"b", "B")]])
        m = r.stages[0].matches[0]
        self.assertEqual(m.source_span, (1, 2))
        self.assertEqual(m.input_span, (1, 2))
        self.assertEqual(m.result_span, (1, 2))

    def test_empty_stage_passes_text_through(self):
        r = run_pipeline("原文", [[]])
        self.assertEqual(r.masked_text, "原文")
        self.assertEqual(r.stages[0].matches, [])


class TestDeletionAndLengthChange(unittest.TestCase):
    def test_deletion_then_hit_across_gap_gives_enclosing_span(self):
        # 删除数字后，后阶段命中跨越删除点：初始原文外包区间包含被删字符
        r = run_pipeline("ab12cd34ef", [
            [rule(0, r"\d+", "")],          # abcdef
            [rule(0, r"bcde", "Z", 10)],
        ])
        self.assertEqual(r.masked_text, "aZf")
        m = r.stages[1].matches[0]
        self.assertEqual(m.source_span, (1, 9))  # 外包含已删除的 12/34
        self.assertFalse(m.generated)

    def test_variable_length_chained_spans(self):
        r = run_pipeline("电话13800001111ok", [
            [rule(0, r"1(\d{10})", r"NUM(\1)")],   # 电话NUM(3800001111)ok
            [rule(0, r"\d{5}", "", priority=10)],  # 电话NUM()ok
            [rule(0, r"NUM\(\)", "PHONE")],        # 电话PHONEok
        ])
        self.assertEqual(r.masked_text, "电话PHONEok")
        d0, d1 = r.stages[1].matches
        self.assertEqual(d0.source_span, (3, 8))
        self.assertEqual(d1.source_span, (8, 13))
        gen = r.stages[2].matches[0]
        self.assertTrue(gen.generated)
        self.assertIsNone(gen.source_span)
        # 各 applied 命中的结果区间确实指向其替换文本
        for st in r.stages:
            for m in st.matches:
                if m.status == "applied":
                    self.assertEqual(
                        st.masked_text[m.result_span[0]:m.result_span[1]],
                        m.replacement,
                    )

    def test_group_zero_is_source_derived(self):
        r = run_pipeline("abc123", [[rule(0, r"\d+", r"[\g<0>]")]])
        m = r.stages[0].matches[0]
        # 整体 [ ] 新生，但内部数字来自原文，外包仍可映射
        self.assertFalse(m.generated)
        self.assertEqual(m.source_span, (3, 6))


class TestOverlapRejection(unittest.TestCase):
    def test_rejected_match_maps_surviving_fragment(self):
        r = run_pipeline("x123456y", [
            [rule(0, r"\d+", "[NN]")],                 # x[NN]y
            [rule(0, r"\[NN\]", "Z", 0),
             rule(1, r"x\[N", "W", 5)],               # W 胜出
        ])
        self.assertEqual(r.stages[0].masked_text, "x[NN]y")
        self.assertEqual(r.masked_text, "WN]y")
        rej = [m for m in r.stages[1].matches
               if m.status == "rejected_overlap"][0]
        win = [m for m in r.stages[1].matches if m.status == "applied"][0]
        # 存活片段 "N]" 完全来自上阶段替换新生
        self.assertEqual(rej.result_span, (1, 3))
        self.assertEqual(r.masked_text[1:3], "N]")
        self.assertTrue(rej.generated)
        self.assertIsNone(rej.source_span)
        # 胜出命中横跨原文 'x' 与新生 '[N'
        self.assertEqual(win.source_span, (0, 1))
        self.assertFalse(win.generated)

    def test_rejected_match_fully_covered_has_empty_span(self):
        r = run_pipeline("a1b2c3d4e5", [
            [rule(0, r"[a-z]\d", "X", 5), rule(1, r".+", "Y")],
        ])
        rej = [m for m in r.stages[0].matches
               if m.status == "rejected_overlap"][0]
        # 空区间吸附到首个覆盖区间（[0,2) -> 输出 [0,1)）的替换落点
        self.assertEqual(rej.result_span, (1, 1))


class TestStageScopedErrors(unittest.TestCase):
    def test_invalid_rule_only_errors_in_its_stage(self):
        r = run_pipeline("abc123", [
            [rule(0, "(", "X"), rule(1, r"\d", "#")],
            [rule(0, r"#", "N")],
            [rule(0, r"N", r"\g<9>")],
        ])
        self.assertEqual(r.masked_text, "abcNNN")
        self.assertEqual(
            [(e.rule_index, e.stage) for e in r.stages[0].rule_errors],
            [(0, "compile")],
        )
        self.assertEqual(r.stages[1].rule_errors, [])
        self.assertEqual(
            [(e.rule_index, e.stage) for e in r.stages[2].rule_errors],
            [(0, "replacement")],
        )
        # 规则作废的阶段文本原样流转到下一阶段
        self.assertEqual(r.stages[2].masked_text, "abcNNN")


if __name__ == "__main__":
    unittest.main()
