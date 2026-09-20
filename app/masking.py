"""核心脱敏逻辑：正则命中收集、重叠区间确定性消解、原文/结果区间映射。

本模块不依赖任何第三方包，可独立测试。

消解规则（确定性）：
    1. 优先级 priority 数值大者优先；
    2. 优先级相同，起始位置靠前者优先；
    3. 起始位置也相同，规则在请求中出现顺序靠前者优先；
    4. 以上全相同（同一规则不可能在同一位置产生两个命中），
       依次以区间更长者、全局发现顺序兜底，保证完全确定。
按上述顺序贪心选出不重叠的“胜出”区间，仅对胜出区间执行替换。

mask_text(track_segments=True) 额外把每个胜出替换拆成「模板字面量 /
命中分组片段」两类来源（_parse_replacement 严格镜像 CPython 替换模板
语法），供 app.pipeline 在多阶段执行时把替换产生的字符继续映射回初始原文。
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Optional

#: 替换模板中被识别为单字符字面量的转义（与 CPython sre_parse.parse_template 一致）
_ESCAPE_LITERALS = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\",
}
_OCTDIGITS = frozenset("01234567")
_DIGITS = frozenset("0123456789")
#: re 分组编号上限
_MAXGROUPS = (1 << 30) - 1


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
    # 仅当 mask_text(track_segments=True) 时填充：替换内容按模板拆成的段，
    # 每段为 ("lit", 字面量文本) 或 ("grp", 组内文本在命中输入中的 [起,止))。
    # 多阶段流水线据此把替换产生的字符继续映射回初始原文。
    repl_segments: Optional[list] = field(default=None)


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


class _BadTemplate(ValueError):
    """替换模板非法（与 re.error 的模板类错误同义，便于调用方统一捕获）。"""


def _parse_replacement(template: str, groupcount: int, groupindex: dict) -> list:
    """把替换模板解析为 token 列表（严格镜像 CPython sre_parse.parse_template）。

    token 形如：
        ("lit", 字面量文本)
        ("grp", 分组编号 int)   # \\1、\\12 或 \\g<..>
    仅用于多阶段溯源；模板合法性仍以 re 的 Match.expand 为准。
    """
    tokens: list = []
    literal: list = []
    n = len(template)
    i = 0

    def flush() -> None:
        if literal:
            tokens.append(("lit", "".join(literal)))
            literal.clear()

    while i < n:
        c = template[i]
        if c != "\\":
            literal.append(c)
            i += 1
            continue
        if i + 1 >= n:
            # 结尾孤立反斜杠：与 sre_parse.Tokenizer 一致，按非法转义处理
            raise _BadTemplate("bad escape (end of pattern)")
        q = template[i + 1]
        if q == "g":  # \g<name|index>
            j = i + 2
            if j >= n or template[j] != "<":
                raise _BadTemplate("missing <")
            k = template.find(">", j + 1)
            if k == -1 or k == j + 1:
                raise _BadTemplate("missing group name")
            name = template[j + 1:k]
            if name.isidentifier():
                if name not in groupindex:
                    raise _BadTemplate("unknown group name %r" % name)
                gi = groupindex[name]
            else:
                try:
                    gi = int(name)
                except ValueError:
                    raise _BadTemplate("bad character in group name %r" % name)
                if gi < 0 or gi >= _MAXGROUPS:
                    raise _BadTemplate("invalid group reference %d" % gi)
            flush()
            tokens.append(("grp", gi))
            i = k + 1
        elif q == "0":  # \0、\0nn：八进制转义，最多三位
            j = i + 2
            if j < n and template[j] in _OCTDIGITS:
                j += 1
                if j < n and template[j] in _OCTDIGITS:
                    j += 1
            literal.append(chr(int(template[i + 1:j], 8) & 0xFF))
            i = j
        elif q in _DIGITS:  # \1、\12 分组引用；\0oo 三位八进制字面量
            j = i + 2
            is_octal = False
            if j < n and template[j] in _DIGITS:
                if (
                    q in _OCTDIGITS
                    and template[j] in _OCTDIGITS
                    and j + 1 < n
                    and template[j + 1] in _OCTDIGITS
                ):
                    val = int(template[i + 1:j + 2], 8)
                    if val > 0o377:
                        raise _BadTemplate(
                            "octal escape value %s outside of range 0-0o377"
                            % template[i:j + 2]
                        )
                    literal.append(chr(val))
                    is_octal = True
                    j += 2
                else:
                    j += 1  # 消费第二个数字，形成两位组引用
            if not is_octal:
                gi = int(template[i + 1:j])
                if gi > groupcount:
                    raise _BadTemplate("invalid group reference %d" % gi)
                flush()
                tokens.append(("grp", gi))
            i = j
        elif q in _ESCAPE_LITERALS:
            literal.append(_ESCAPE_LITERALS[q])
            i += 2
        elif q.isascii() and q.isalpha():
            raise _BadTemplate("bad escape \\%s" % q)
        else:  # \$ 等未识别转义：原样保留两个字符
            literal.append("\\")
            literal.append(q)
            i += 2
    flush()
    return tokens


def _resolve_segments(m: re.Match, tokens: list) -> tuple:
    """依据命中 m 展开模板 token，返回 (展开文本, 溯源段列表)。

    溯源段每段为 ("lit", 文本) 或 ("grp", m 输入中的相对 [起,止))；
    未参与匹配的可选组跨度为空，不产生任何字符。
    """
    parts: list = []
    segments: list = []
    for kind, val in tokens:
        if kind == "lit":
            if val:
                parts.append(val)
                segments.append(("lit", val))
        else:
            gs, ge = m.span(val)
            if gs >= 0 and ge > gs:
                text = m.string[gs:ge]
                parts.append(text)
                # 转成相对命中起点的偏移
                segments.append(("grp", (gs - m.start(), ge - m.start())))
    return "".join(parts), segments


def mask_text(
    text: str, rules: list, track_segments: bool = False
) -> MaskResult:
    """对 text 按 rules 执行脱敏，返回结果文本、全部命中状态及区间映射。

    track_segments=True 时（多阶段流水线使用），额外为每个胜出命中填充
    repl_segments：把替换内容拆成「模板字面量 / 命中分组片段」两类，
    供调用方把替换新生的字符继续映射回初始原文。
    """
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
        tokens = None           # 延迟到首个命中再解析模板（非法时整规则作废）
        for m in pattern.finditer(text):
            if m.start() == m.end():
                continue  # 跳过零宽命中，避免无意义插入
            try:
                expanded = m.expand(rule.replacement)
                if track_segments:
                    if tokens is None:
                        tokens = _parse_replacement(
                            rule.replacement, pattern.groups, pattern.groupindex
                        )
                    expanded, segments = _resolve_segments(m, tokens)
                else:
                    segments = None
            # expand 对未知 \g<name> 在部分 Python 版本抛 IndexError
            except (re.error, IndexError, _BadTemplate) as exc:
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
                    repl_segments=segments,
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

    # 4) 被否决命中的结果区间：取其未被任何胜出区间覆盖的存活片段，
    #    映射到结果文本后取外包区间；被完全覆盖时为空区间（吸附到覆盖其起点的
    #    胜出替换落点处）。
    def _map_position(x: int) -> int:
        i = bisect.bisect_right(accepted_starts, x) - 1
        if i >= 0 and accepted[i].end > x:
            return accepted[i].result_end
        k = bisect.bisect_right(prefix_ends, x)
        return x + prefix_delta[k]

    def _rejected_result_span(c: Candidate) -> tuple:
        landings: list = []
        # 与 [c.start, c.end) 相交的胜出区间按 start 升序遍历，逐段收集存活片段
        i = bisect.bisect_right(accepted_starts, c.start) - 1
        if i >= 0 and accepted[i].end > c.start:
            first = i  # 左邻已覆盖 c.start，必须计入
        else:
            first = i + 1
        pos = c.start
        anchor = None  # 完全被覆盖时的空区间吸附点（首个覆盖 c.start 的区间落点）
        for a in accepted[first:]:
            if a.start >= c.end:
                break
            if anchor is None:
                anchor = a.result_end
            if a.start > pos:  # 与前一胜出区间之间存活的原片段
                landings.append((_map_position(pos), _map_position(a.start)))
            pos = max(pos, a.end)
        if pos < c.end:  # 末尾存活的原片段
            landings.append((_map_position(pos), _map_position(c.end)))
        if not landings:
            return anchor, anchor  # 全部内容被胜出区间覆盖：空区间吸附
        return min(lo for lo, _ in landings), max(hi for _, hi in landings)

    for c in candidates:
        if c.applied:
            continue
        c.result_start, c.result_end = _rejected_result_span(c)

    matches = sorted(candidates, key=lambda c: (c.start, c.end, c.rule_index))
    applied_count = sum(1 for c in candidates if c.applied)
    return MaskResult(
        masked_text=masked,
        matches=matches,
        rule_errors=rule_errors,
        applied_count=applied_count,
        rejected_count=len(candidates) - applied_count,
    )
