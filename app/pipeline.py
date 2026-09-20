"""多阶段脱敏流水线：顺序执行多组规则，并把每个阶段命中的区间映射回初始原文。

设计要点：
    - 每阶段直接复用 :func:`app.masking.mask_text`（同一套规则格式、同一套
      确定性重叠消解），以上一阶段输出文本作为本阶段输入；
    - 维护「逐字符溯源链」``origins``：阶段文本中每个字符对应初始原文的偏移，
      替换模板新生成的字符（字面量、转义等）记为 None；
    - 替换模板中的分组引用（如 \\1、\\g<name>）展开的是命中片段，其字符继承
      命中位置的溯源（由 ``track_segments`` 提供逐段来源）；
    - 任意区间映射回初始原文时，取其覆盖字符溯源偏移的最小外包区间；
      若整段全部由替换新生（溯源全为 None），返回 None 并标记 generated=True。

这样在「替换变长 / 删除 / 重叠否决」共同出现时，映射仍可逐阶段正确组合。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .masking import RuleInput, mask_text


@dataclass
class PipelineMatch:
    """单个阶段内的一个命中（区间同时相对阶段输入与初始原文）。"""

    stage_index: int
    rule_index: int
    rule_id: Optional[str]
    status: str                 # "applied" / "rejected_overlap"
    input_span: tuple           # [start, end)，阶段输入文本中的区间
    result_span: tuple          # [start, end)，阶段输出文本中的区间
    source_span: Optional[tuple]  # [start, end)，初始原文中的外包区间；纯新生为 None
    generated: bool             # 整段是否完全由替换新生、无法对应初始原文
    matched_text: str
    replacement: Optional[str]
    reason: Optional[str]
    overlapped_by: Optional[dict]


@dataclass
class StageResult:
    """一个阶段的执行结果。"""

    stage_index: int
    input_length: int
    masked_text: str
    matches: list               # list[PipelineMatch]
    rule_errors: list           # list[RuleErrorInfo]
    applied_count: int
    rejected_count: int
    rules_total: int


@dataclass
class PipelineResult:
    masked_text: str
    stages: list                # list[StageResult]


def _compose_origins(
    text: str,
    accepted: list,
    origins: Optional[list],
) -> list:
    """根据本阶段胜出区间，由阶段输入的 origins 组合出阶段输出的逐字符溯源。

    - 未被替换覆盖的字符：直接继承输入 origins（无前置阶段时为自身偏移）；
    - 替换内容中来自命中分组的字符：继承命中位置的 origins；
    - 替换内容中的模板字面量：None（替换新生）。
    """
    out: list = []
    cursor = 0
    for a in accepted:
        # 胜出区间之前存活的原文片段
        for p in range(cursor, a.start):
            out.append(origins[p] if origins is not None else p)
        # 替换内容逐段溯源
        for kind, val in a.repl_segments:
            if kind == "lit":
                out.extend([None] * len(val))
            else:
                gs, ge = val  # 相对命中起点的 [起,止)
                for p in range(a.start + gs, a.start + ge):
                    out.append(origins[p] if origins is not None else p)
        cursor = a.end
    for p in range(cursor, len(text)):
        out.append(origins[p] if origins is not None else p)
    return out


def _map_span(start: int, end: int, origins: Optional[list]) -> tuple:
    """把阶段输入区间 [start,end) 映射回初始原文。

    返回 (外包区间或 None, generated)。区间内所有字符均无初始原文溯源时
    generated=True；否则外包区间可能因删除/重排而宽于逐段实际覆盖范围。
    """
    if start == end:
        return None, True
    mapped = [
        origins[p] if origins is not None else p
        for p in range(start, end)
    ]
    mapped = [o for o in mapped if o is not None]
    if not mapped:
        return None, True
    return (min(mapped), max(mapped) + 1), False


def run_pipeline(text: str, stages: list) -> PipelineResult:
    """顺序执行多阶段脱敏。

    stages 为 list[list[RuleInput]]；每个阶段只报本阶段的非法规则，
    不影响其他规则与后续阶段。
    """
    stage_results: list = []
    current = text
    origins: Optional[list] = None  # 第一阶段输入即初始原文，偏移恒等，用 None 表示

    for si, rules in enumerate(stages):
        result = mask_text(current, rules, track_segments=True)

        # 胜出区间按 start 升序（mask_text 内部即按此顺序应用）
        accepted = [c for c in result.matches if c.applied]
        out_origins = _compose_origins(current, accepted, origins)

        matches: list = []
        for c in result.matches:
            source_span, generated = _map_span(c.start, c.end, origins)
            if c.applied:
                reason = None
                overlapped = None
                replacement = c.replacement
            else:
                reason = (
                    f"与胜出区间 [{c.winner.start}, {c.winner.end})"
                    f"（规则 #{c.winner.rule_index}）重叠，被否决"
                )
                overlapped = {
                    "rule_index": c.winner.rule_index,
                    "rule_id": c.winner.rule_id,
                    "span": (c.winner.start, c.winner.end),
                }
                replacement = None
            matches.append(
                PipelineMatch(
                    stage_index=si,
                    rule_index=c.rule_index,
                    rule_id=c.rule_id,
                    status="applied" if c.applied else "rejected_overlap",
                    input_span=(c.start, c.end),
                    result_span=(c.result_start, c.result_end),
                    source_span=source_span,
                    generated=generated,
                    matched_text=c.matched_text,
                    replacement=replacement,
                    reason=reason,
                    overlapped_by=overlapped,
                )
            )

        stage_results.append(
            StageResult(
                stage_index=si,
                input_length=len(current),
                masked_text=result.masked_text,
                matches=matches,
                rule_errors=result.rule_errors,
                applied_count=result.applied_count,
                rejected_count=result.rejected_count,
                rules_total=len(rules),
            )
        )
        current = result.masked_text
        origins = out_origins

    return PipelineResult(masked_text=current, stages=stage_results)
