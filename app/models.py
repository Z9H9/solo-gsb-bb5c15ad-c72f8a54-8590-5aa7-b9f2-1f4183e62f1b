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


class HealthResponse(BaseModel):
    status: str = "ok"
