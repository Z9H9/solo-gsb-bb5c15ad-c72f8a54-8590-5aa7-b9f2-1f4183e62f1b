"""核心脱敏逻辑：正则命中收集、重叠区间确定性消解、原文/结果区间映射。

本模块不依赖任何第三方包，可独立测试。

消解规则（确定性）：
    1. 优先级 priority 数值大者优先；
    2. 优先级相同，起始位置靠前者优先；
    3. 起始位置也相同，规则在请求中出现顺序靠前者优先；
    4. 以上全相同（同一规则不可能在同一位置产生两个命中），
       依次以区间更长者、全局发现顺序兜底，保证完全确定。
按上述顺序贪心选出不重叠的“胜出”区间，仅对胜出区间执行替换。
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class RuleInput:
    """一条脱敏规则的内部表示。"""

    index: int            # 规则在请求中的顺序（0 起）
    rule_id: Optional[str]
    pattern: str
    priority: int
    replacement: str


@dataclass
class RuleErrorInfo:
    """单条规则的错误信息（不影响其他规则）。"""

    rule_index: int
    rule_id: Optional[str]
    pattern: str
    stage: str            # "compile"（正则非法）或 "replacement"（替换模板非法）
    error: str


@dataclass
class Candidate:
    """一个正则命中区间及其消解结果。"""

    rule_index: int
    rule_id: Optional[str]
    priority: int
    start: int            # 原文区间 [start, end)
    end: int
    matched_text: str
    replacement: str      # 模板展开后的替换内容
    discovery_order: int  # 全局发现顺序，用于兜底保证确定性
    applied: bool = False
    winner: Optional["Candidate"] = None  # 否决本命中的胜出区间
    result_start: int = 0  # 对应结果文本区间 [result_start, result_end)
    result_end: int = 0


@dataclass
class MaskResult:
    masked_text: str
    matches: list                 # list[Candidate]，按 (start, end, rule_index) 排序
    rule_errors: list             # list[RuleErrorInfo]
    applied_count: int
    rejected_count: int


def _resolve_order_key(c: Candidate) -> tuple:
    """消解排序键：优先级降序 → 起始位置升序 → 规则顺序升序 → 区间长度降序 → 发现顺序。"""
    return (-c.priority, c.start, c.rule_index, -(c.end - c.start), c.discovery_order)


def _find_overlap(accepted: list, accepted_starts: list, c: Candidate) -> Optional[Candidate]:
    """在已胜出（互不重叠且按 start 有序）的区间中查找与 c 重叠者，无则返回 None。"""
    i = bisect.bisect_right(accepted_starts, c.start) - 1
    # 左侧邻居：start <= c.start，若其 end > c.start 则重叠
    if i >= 0 and accepted[i].end > c.start:
        return accepted[i]
    # 右侧邻居：start > c.start，若其 start < c.end 则重叠
    j = i + 1
    if j < len(accepted) and accepted[j].start < c.end:
        return accepted[j]
    return None


def mask_text(text: str, rules: list) -> MaskResult:
    """对 text 按 rules 执行脱敏，返回结果文本、全部命中状态及区间映射。"""
    rule_errors: list = []
    candidates: list = []
    order = 0

    # 1) 编译规则并收集全部命中；非法规则逐条记录错误，不影响其他规则
    for rule in rules:
        try:
            pattern = re.compile(rule.pattern)
        except re.error as exc:
            rule_errors.append(
                RuleErrorInfo(rule.index, rule.rule_id, rule.pattern, "compile", str(exc))
            )
            continue

        base = len(candidates)  # 本规则命中在 candidates 中的起始下标
        for m in pattern.finditer(text):
            if m.start() == m.end():
                continue  # 跳过零宽命中，避免无意义插入
            try:
                expanded = m.expand(rule.replacement)
            except re.error as exc:
                # 替换模板非法（如引用了不存在的分组）：整规则作废并回滚已收集命中
                del candidates[base:]
                rule_errors.append(
                    RuleErrorInfo(rule.index, rule.rule_id, rule.pattern, "replacement", str(exc))
                )
                break
            candidates.append(
                Candidate(
                    rule_index=rule.index,
                    rule_id=rule.rule_id,
                    priority=rule.priority,
                    start=m.start(),
                    end=m.end(),
                    matched_text=m.group(0),
                    replacement=expanded,
                    discovery_order=order,
                )
            )
            order += 1

    # 2) 按确定性顺序贪心消解重叠
    candidates.sort(key=_resolve_order_key)
    accepted: list = []          # 胜出区间，始终按 start 升序
    accepted_starts: list = []
    for c in candidates:
        winner = _find_overlap(accepted, accepted_starts, c)
        if winner is None:
            c.applied = True
            pos = bisect.bisect_right(accepted_starts, c.start)
            accepted.insert(pos, c)
            accepted_starts.insert(pos, c.start)
        else:
            c.winner = winner

    # 3) 依胜出区间拼接结果文本，并记录胜出区间的结果位置
    parts: list = []
    cursor = 0
    delta = 0                    # 已处理区间造成的长度差累计
    prefix_ends: list = []       # 各胜出区间的 end（有序）
    prefix_delta: list = [0]     # prefix_delta[k] = 前 k 个胜出区间累计的 delta
    for a in accepted:
        parts.append(text[cursor:a.start])
        a.result_start = a.start + delta
        parts.append(a.replacement)
        a.result_end = a.result_start + len(a.replacement)
        delta += len(a.replacement) - (a.end - a.start)
        prefix_ends.append(a.end)
        prefix_delta.append(delta)
        cursor = a.end
    parts.append(text[cursor:])
    masked = "".join(parts)

    # 4) 被否决命中的结果区间：位置映射（落在已替换区间内的位置右吸附到替换内容
    #    末尾），使结果区间恰好指向该命中在结果文本中存活的片段
    def _map_position(x: int) -> int:
        i = bisect.bisect_right(accepted_starts, x) - 1
        if i >= 0 and accepted[i].end > x:
            return accepted[i].result_end
        k = bisect.bisect_right(prefix_ends, x)
        return x + prefix_delta[k]

    for c in candidates:
        if c.applied:
            continue
        c.result_start = _map_position(c.start)
        c.result_end = _map_position(c.end)

    matches = sorted(candidates, key=lambda c: (c.start, c.end, c.rule_index))
    applied_count = sum(1 for c in candidates if c.applied)
    return MaskResult(
        masked_text=masked,
        matches=matches,
        rule_errors=rule_errors,
        applied_count=applied_count,
        rejected_count=len(candidates) - applied_count,
    )
