"""API 请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Rule(BaseModel):
    """一条脱敏规则。"""

    id: Optional[str] = Field(
        default=None, description="客户端自定义规则标识，响应中原样返回，便于对账"
    )
    pattern: str = Field(min_length=1, description="正则表达式（Python re 语法）")
    priority: int = Field(
        default=0, description="优先级，数值越大越优先；重叠消解时高优先级命中胜出"
    )
    replacement: str = Field(
        description="替换模板，支持分组引用，如 \\1、\\g<name>；空字符串表示删除命中内容"
    )


class DesensitizeRequest(BaseModel):
    text: str = Field(description="待脱敏原文")
    rules: list[Rule] = Field(default_factory=list, description="脱敏规则列表")


class PipelineStage(BaseModel):
    """流水线中的一个阶段：复用单阶段规则格式，处理上一阶段输出文本。"""

    rules: list[Rule] = Field(default_factory=list, description="本阶段脱敏规则列表")


class PipelineRequest(BaseModel):
    text: str = Field(description="待脱敏初始原文")
    stages: list[PipelineStage] = Field(
        min_length=1,
        description="按顺序执行的脱敏阶段（至少 1 个）；首阶段处理初始原文，后续阶段处理上一阶段输出",
    )


class Span(BaseModel):
    start: int = Field(description="区间起始偏移（含），按字符计")
    end: int = Field(description="区间结束偏移（不含），按字符计")


class OverlappedBy(BaseModel):
    rule_index: int
    rule_id: Optional[str]
    span: Span


class MatchResult(BaseModel):
    rule_index: int = Field(description="规则在请求 rules 中的下标")
    rule_id: Optional[str]
    status: Literal["applied", "rejected_overlap"] = Field(
        description="applied=已替换；rejected_overlap=因与胜出区间重叠被否决"
    )
    original_span: Span = Field(description="命中在原文中的区间")
    result_span: Span = Field(description="该命中对应内容在脱敏结果中的区间")
    matched_text: str = Field(description="命中的原文片段")
    replacement: Optional[str] = Field(
        description="实际写入结果的替换内容（模板展开后）；被否决时为 null"
    )
    reason: Optional[str] = Field(description="被否决原因说明；applied 时为 null")
    overlapped_by: Optional[OverlappedBy] = Field(
        description="否决本命中的胜出区间信息；applied 时为 null"
    )


class RuleError(BaseModel):
    rule_index: int
    rule_id: Optional[str]
    pattern: str
    stage: Literal["compile", "replacement"] = Field(
        description="compile=正则编译失败；replacement=替换模板非法"
    )
    error: str


class Stats(BaseModel):
    text_length: int
    masked_length: int
    rules_total: int
    rules_invalid: int
    matches_found: int
    matches_applied: int
    matches_rejected: int


class DesensitizeResponse(BaseModel):
    masked_text: str = Field(description="脱敏结果文本")
    matches: list[MatchResult] = Field(
        description="全部命中（按原文区间排序），含每个命中的处理状态与原文/结果区间映射"
    )
    rule_errors: list[RuleError] = Field(
        description="非法规则逐条报错，不影响其他规则"
    )
    stats: Stats


class PipelineMatchResult(BaseModel):
    """流水线某阶段中的一个命中。"""

    stage_index: int = Field(description="所属阶段下标（0 起）")
    rule_index: int = Field(description="规则在所属阶段 rules 中的下标")
    rule_id: Optional[str]
    status: Literal["applied", "rejected_overlap"]
    input_span: Span = Field(
        description="命中区间（阶段输入文本中，即上一阶段输出）"
    )
    result_span: Span = Field(
        description="命中对应内容在本阶段输出文本中的区间"
    )
    source_span: Optional[Span] = Field(
        description=(
            "命中内容映射回初始原文的区间（取覆盖字符溯源偏移的最小外包区间，"
            "可能因删除/重排而宽于实际逐字覆盖）；"
            "若整段完全由替换新生、无法对应初始原文则为 null"
        )
    )
    generated: bool = Field(
        description="true=整段完全由前序替换新生，初始原文中无对应区间"
    )
    matched_text: str = Field(description="命中的阶段输入片段")
    replacement: Optional[str] = Field(
        description="实际写入本阶段输出的替换内容（模板展开后）；被否决时为 null"
    )
    reason: Optional[str]
    overlapped_by: Optional[OverlappedBy]


class StageResult(BaseModel):
    stage_index: int
    masked_text: str = Field(description="本阶段输出文本（即下一阶段输入）")
    matches: list[PipelineMatchResult] = Field(
        description="本阶段全部命中（按阶段输入区间排序）"
    )
    rule_errors: list[RuleError] = Field(
        description="本阶段非法规则逐条报错；非法规则仅在所属阶段报错，不影响后续阶段"
    )
    stats: Stats


class PipelineStats(BaseModel):
    text_length: int
    masked_length: int
    stages_total: int
    rules_total: int = Field(description="各阶段规则数之和")
    rules_invalid: int = Field(description="各阶段非法规则数之和")
    matches_found: int = Field(description="各阶段命中数之和（含被否决）")
    matches_applied: int = Field(description="各阶段已替换命中数之和")
    matches_rejected: int = Field(description="各阶段被否决命中数之和")


class PipelineResponse(BaseModel):
    masked_text: str = Field(description="最终文本（最后一个阶段的输出）")
    stages: list[StageResult] = Field(description="逐阶段结果，按请求顺序")
    stats: PipelineStats


class HealthResponse(BaseModel):
    status: str = "ok"
