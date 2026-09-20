"""核心脱敏逻辑：正则命中收集、重叠区间确定性消解、原文/结果区间映射。

本模块不依赖任何第三方包，可独立测试。

消解规则（确定性）：
    1. 优先级 priority 数值大者优先；
    2. 优先级相同，起始位置靠前者优先；
    3. 起始位置也相同，规则在请求中出现顺序靠前者优先；
    4. 以上全相同（同一规则不可能在同一位置产生两个命中），
       依次以区间更长者、全局发现顺序兜底，保证完全确定。
按上述顺序贪心选出不重叠的“胜出”区间，仅对胜出区间执行替换。

多阶段流水线（mask_pipeline）在单阶段逻辑之上维护“出处覆盖”（provenance
coverage）：把每一阶段文本切分成互不重叠的段，每段要么 1:1 对应初始原文的一段
（kind="orig"，并记录其在初始原文中的起始偏移），要么是替换新生内容
（kind="gen"）。这样：
  * 变长、删除导致的位置平移由 1:1 覆盖段自然吸收；
  * 重叠否决不影响覆盖，只影响命中状态，命中的出处用覆盖段取原文外包区间；
  * 后续阶段命中前序替换的新生内容时，覆盖段中无任何 orig 字符，
    原文区间返回 None 且 generated=True。
替换模板中的分组回填（如 \\1、\\g<name>）其内容本身是命中文本的子串，因此按
orig 段传播；模板中的字面量（含转义展开结果）一律按 gen 段传播。
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
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
class PipelineRuleErrorInfo(RuleErrorInfo):
    """多阶段流水线中单条规则的错误信息，额外携带所属阶段下标。"""

    stage_index: int = 0


@dataclass
class Candidate:
    """一个正则命中区间及其消解结果。"""

    rule_index: int
    rule_id: Optional[str]
    priority: int
    start: int            # 本阶段输入区间 [start, end)
    end: int
    matched_text: str
    replacement: str      # 模板展开后的替换内容
    discovery_order: int  # 全局发现顺序，用于兜底保证确定性
    applied: bool = False
    winner: Optional["Candidate"] = None  # 否决本命中的胜出区间
    result_start: int = 0  # 对应本阶段结果文本区间 [result_start, result_end)
    result_end: int = 0
    # 替换内容的出处片段：("lit", text)=模板字面量（新生）；
    # ("orig", (group_start, group_end), text)=分组回填（取自本阶段输入）
    pieces: list = field(default_factory=list)
    # 映射回初始原文的外包区间 [source_start, source_end)；无对应原文时为 None
    source_start: Optional[int] = None
    source_end: Optional[int] = None
    generated: bool = False  # 命中区间是否完全由替换新生、无法对应初始原文


@dataclass
class MaskResult:
    masked_text: str
    matches: list                 # list[Candidate]，按 (start, end, rule_index) 排序
    rule_errors: list             # list[RuleErrorInfo]
    applied_count: int
    rejected_count: int


@dataclass
class StageResultInfo:
    """流水线中单个阶段的处理结果。"""

    index: int                    # 阶段下标（0 起）
    input_text: str               # 本阶段输入（即上一阶段输出）
    output_text: str              # 本阶段输出
    matches: list                 # list[Candidate]，按本阶段输入区间排序
    rule_errors: list             # list[PipelineRuleErrorInfo]
    applied_count: int
    rejected_count: int
    rules_total: int


@dataclass
class PipelineMaskResult:
    original_text: str
    final_text: str
    stages: list                  # list[StageResultInfo]
    rules_total: int
    rules_invalid: int
    matches_found: int
    matches_applied: int
    matches_rejected: int


class TextTooLongError(Exception):
    """流水线中间文本或最终文本超过长度上限。"""

    def __init__(self, stage_index: int, length: int, limit: int):
        self.stage_index = stage_index  # 产生超长文本的阶段下标（0 起）
        self.length = length
        self.limit = limit
        super().__init__(
            f"第 {stage_index + 1} 阶段输出长度 {length} 超过上限 {limit}"
        )


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


# ---- 替换模板的出处切分（与 CPython re 的 parse_template 规则保持一致） ---------

_ASCII_DIGITS = frozenset("0123456789")
_OCT_DIGITS = frozenset("01234567")


class _BadTemplate(Exception):
    """缓存槽位：模板对指定分组数非法（错误信息延迟到 expand 时抛出）。"""


# 模板出处操作缓存：(groupcount, template) -> ops；
# m.expand 每次调用都会重新解析模板，命中很多时开销显著，故此处只解析一次。
_TEMPLATE_OPS_CACHE: dict = {}
_TEMPLATE_OPS_CACHE_MAX = 4096


def _get_template_ops(groupcount: int, template: str) -> list:
    key = (groupcount, template)
    ops = _TEMPLATE_OPS_CACHE.get(key)
    if ops is None:
        try:
            ops = _scan_template_ops(template, groupcount)
        except re.error:
            ops = _BadTemplate  # 非法模板仍交由 m.expand 抛出权威错误信息
        if len(_TEMPLATE_OPS_CACHE) < _TEMPLATE_OPS_CACHE_MAX:
            _TEMPLATE_OPS_CACHE[key] = ops
    return ops


def _scan_template_ops(template: str, groupcount: int) -> list:
    """把替换模板切成 ("lit", 原文) 与 ("grp", 组号:int|组名:str) 序列。

    仅识别分组引用（数字组、\\g<name>、\\g<123>），其余内容原样保留在 lit 中，
    其转义展开交由 re.Match.expand 完成，从而与替换结果严格一致。
    数字引用与八进制转义的消歧按 CPython parse_template 规则：
      * \\0 后接至多两个八进制数字 → 八进制转义（lit）；
      * \\1-\\7 后贪婪消费至多两个数字，若首位为 1-3 且后两位均为八进制数字，
        则整体为八进制转义（lit，超 0o377 报错）；否则为分组引用；
      * 分组号超界不回退截断，直接报错。
    """
    ops: list = []
    buf: list = []
    i = 0
    n = len(template)

    def _flush() -> None:
        if buf:
            ops.append(("lit", "".join(buf)))
            buf.clear()

    while i < n:
        ch = template[i]
        if ch != "\\":
            buf.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            raise re.error("bad escape (end of pattern)")
        c = template[i + 1]
        j = i + 2
        if c == "g" and template[j:j + 1] == "<":
            end = template.find(">", j + 1)
            if end < 0:
                raise re.error("missing >, unterminated group name")
            name = template[j + 1:end]
            if not name:
                raise re.error("missing group name")
            if not name.isidentifier():
                # 非标识符名称只能是非负整数组号（非法字符/负数/越界由 expand 报错）
                if not name or not all(d in _ASCII_DIGITS for d in name):
                    raise re.error(f"bad character in group name {name!r}")
                gid: object = int(name)
            else:
                gid = name
            _flush()
            ops.append(("grp", gid))
            i = end + 1
        elif c == "0":
            # \0 八进制转义：再消费至多两个八进制数字，全部留在 lit
            k = j
            while k < n and k < j + 2 and template[k] in _OCT_DIGITS:
                k += 1
            buf.append(template[i:k])
            i = k
        elif c in _ASCII_DIGITS:
            # \1-\9：tokenizer 先无条件再消费一位数字（若有）
            k = j
            if k < n and template[k] in _ASCII_DIGITS:
                k += 1
            raw = template[i + 1:k]
            # 已消费两位且都是八进制数字，且第三位也是八进制数字：三位八进制转义
            if (
                len(raw) == 2
                and raw[0] in _OCT_DIGITS
                and raw[1] in _OCT_DIGITS
                and k < n
                and template[k] in _OCT_DIGITS
            ):
                k += 1
                raw = template[i + 1:k]
                if int(raw, 8) > 0o377:
                    raise re.error(
                        f"octal escape value \\{raw} outside of range 0-0o377"
                    )
                buf.append(template[i:k])
                i = k
            else:
                gid = int(raw)
                if gid > groupcount:
                    raise re.error(f"invalid group reference {gid}")
                _flush()
                ops.append(("grp", gid))
                i = k
        else:
            # 其他转义（\\、\n、\xHH 等）：原样保留，展开交给 expand
            buf.append("\\")
            buf.append(c)
            i = j
    _flush()
    return ops


def _build_pieces(m: re.Match, template: str) -> list:
    """展开替换模板并给出每个输出片段相对本阶段输入的出处。"""
    ops = _get_template_ops(m.re.groups, template)
    pieces: list = []
    for kind, val in ops:
        if kind == "lit":
            # 无分组引用的子模板：expand 负责转义展开（此处不会再报错，
            # 因为整个模板的 m.expand 已先成功执行）
            pieces.append(("lit", m.expand(val)))
            continue
        try:
            gv = m.group(val)
            gs, ge = m.span(val)
        except (IndexError, re.error) as exc:
            raise re.error(str(exc))
        if gv is None or gv == "":
            continue  # 未参与或空匹配的分组不产生输出
        pieces.append(("orig", (gs, ge), gv))
    return pieces


def _collect_and_resolve(text: str, rules: list, error_factory=RuleErrorInfo):
    """单阶段处理：收集命中、确定性消解重叠、拼接结果、计算阶段内区间映射。

    返回 (masked, matches, accepted, rule_errors, applied_count, rejected_count)。
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
                error_factory(rule.index, rule.rule_id, rule.pattern, "compile", str(exc))
            )
            continue

        base = len(candidates)  # 本规则命中在 candidates 中的起始下标
        for m in pattern.finditer(text):
            if m.start() == m.end():
                continue  # 跳过零宽命中，避免无意义插入
            try:
                expanded = m.expand(rule.replacement)
                pieces = _build_pieces(m, rule.replacement)
            except (re.error, IndexError) as exc:
                # 替换模板非法（如引用了不存在的分组）：整规则作废并回滚已收集命中。
                # 注意：Python 3.11 对不存在的命名组抛 IndexError，3.12 起统一为 re.error。
                del candidates[base:]
                rule_errors.append(
                    error_factory(
                        rule.index, rule.rule_id, rule.pattern, "replacement", str(exc)
                    )
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
                    pieces=pieces,
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
    return (
        masked,
        matches,
        accepted,
        rule_errors,
        applied_count,
        len(candidates) - applied_count,
    )


def mask_text(text: str, rules: list) -> MaskResult:
    """对 text 按 rules 执行脱敏，返回结果文本、全部命中状态及区间映射。"""
    masked, matches, _accepted, rule_errors, applied_count, rejected_count = (
        _collect_and_resolve(text, rules)
    )
    return MaskResult(
        masked_text=masked,
        matches=matches,
        rule_errors=rule_errors,
        applied_count=applied_count,
        rejected_count=rejected_count,
    )


# ---- 多阶段流水线：出处覆盖（provenance coverage） -----------------------------
#
# 覆盖段为 (start, end, kind, origin)：
#   * 在“当前阶段文本”中占据 [start, end)（段内连续且长度与输入字符一一对应）；
#   * kind="orig" 时 origin 为该段首字符在初始原文中的偏移，段内第 t 个字符对应
#     初始原文 origin+t；kind="gen" 时 origin 为 None。


class _Coverage:
    """对一版文本的出处覆盖段建立索引，支持区间切片与原文外包查询。"""

    __slots__ = ("segs", "starts", "ends", "orig_segs", "orig_starts", "orig_ends")

    def __init__(self, segs: list):
        self.segs = segs
        self.starts = [s[0] for s in segs]
        self.ends = [s[1] for s in segs]
        self.orig_segs = [s for s in segs if s[2] == "orig"]
        self.orig_starts = [s[0] for s in self.orig_segs]
        self.orig_ends = [s[1] for s in self.orig_segs]

    def slice(self, start: int, end: int) -> list:
        """返回与 [start, end) 相交的全部覆盖段（不裁剪端点）。"""
        i = bisect.bisect_right(self.ends, start)
        j = bisect.bisect_left(self.starts, end)
        return self.segs[i:j]

    def origin_envelope(self, start: int, end: int):
        """[start,end) 内出现的初始原文坐标外包区间 (lo, hi)；全为新生内容时 None。"""
        i = bisect.bisect_right(self.orig_ends, start)
        if i >= len(self.orig_segs) or self.orig_segs[i][0] >= end:
            return None
        j = bisect.bisect_left(self.orig_starts, end) - 1
        s, e, _k, o = self.orig_segs[i]
        lo = o + (max(s, start) - s)
        s2, e2, _k2, o2 = self.orig_segs[j]
        hi = o2 + (min(e2, end) - s2)
        return lo, hi


def _append_seg(out: list, seg: tuple) -> None:
    """追加覆盖段，相邻且出处连续的同类段自动合并，控制段总数。"""
    if out:
        ps, pe, pk, po = out[-1]
        ns, ne, nk, no = seg
        if pe == ns and pk == nk and (pk == "gen" or no == po + (pe - ps)):
            out[-1] = (ps, ne, pk, po)
            return
    out.append(seg)


def _copy_untouched(
    coverage: _Coverage,
    lo: int,
    hi: int,
    in_base: int,
    out_base: int,
    out: list,
) -> None:
    """把输入 [lo,hi)（相对 in_base 的未改动区）平移拷贝到输出 out_base 起。"""
    for s, e, kind, o in coverage.slice(lo, hi):
        sublo = max(s, lo)
        subhi = min(e, hi)
        ns = out_base + (sublo - in_base)
        if kind == "orig":
            _append_seg(out, (ns, ns + (subhi - sublo), "orig", o + (sublo - s)))
        else:
            _append_seg(out, (ns, ns + (subhi - sublo), "gen", None))


def _build_output_coverage(
    coverage: _Coverage, accepted: list, input_length: int
) -> list:
    """根据胜出替换及其出处片段，构造阶段输出文本的出处覆盖。"""
    out: list = []
    in_cur = 0   # 已处理到的输入位置
    out_cur = 0  # 已生成到的输出位置
    for a in accepted:
        # 替换前的未改动文本：1:1 平移
        if a.start > in_cur:
            _copy_untouched(coverage, in_cur, a.start, in_cur, out_cur, out)
            out_cur += a.start - in_cur
            in_cur = a.start
        # 替换内容：字面量=新生段；分组回填=按命中区间内的旧覆盖段传播
        for piece in a.pieces:
            if piece[0] == "lit":
                text = piece[1]
                _append_seg(out, (out_cur, out_cur + len(text), "gen", None))
                out_cur += len(text)
            else:
                (gs, ge), text = piece[1], piece[2]
                for s, e, kind, o in coverage.slice(gs, ge):
                    sublo = max(s, gs)
                    subhi = min(e, ge)
                    ns = out_cur + (sublo - gs)
                    if kind == "orig":
                        _append_seg(
                            out,
                            (ns, ns + (subhi - sublo), "orig", o + (sublo - s)),
                        )
                    else:
                        _append_seg(out, (ns, ns + (subhi - sublo), "gen", None))
                out_cur += ge - gs
        in_cur = a.end
    # 尾部未改动文本
    if input_length > in_cur:
        _copy_untouched(coverage, in_cur, input_length, in_cur, out_cur, out)
    return out


def _pipeline_error(stage_index: int):
    def _factory(rule_index, rule_id, pattern, stage, error):
        return PipelineRuleErrorInfo(
            rule_index=rule_index,
            rule_id=rule_id,
            pattern=pattern,
            stage=stage,
            error=error,
            stage_index=stage_index,
        )

    return _factory


def mask_pipeline(
    text: str,
    stage_rules: list,
    max_length: Optional[int] = None,
) -> PipelineMaskResult:
    """按顺序对 text 执行多个脱敏阶段，返回最终文本、逐阶段结果及原文出处映射。

    stage_rules: 每阶段一个 list[RuleInput]；每阶段处理上一阶段的输出。
    max_length: 初始/中间/最终文本的字符数上限，超出抛 TextTooLongError。
    """
    if max_length is not None and len(text) > max_length:
        raise TextTooLongError(-1, len(text), max_length)

    current = text
    coverage = _Coverage([(0, len(text), "orig", 0)] if text else [])
    stages: list = []

    rules_total = 0
    rules_invalid = 0
    matches_found = 0
    matches_applied = 0
    matches_rejected = 0

    for si, rules in enumerate(stage_rules):
        masked, matches, accepted, errors, applied, rejected = _collect_and_resolve(
            current, rules, _pipeline_error(si)
        )

        # 每个命中映射回初始原文：覆盖段中存在 orig 字符则取其外包区间，
        # 否则该命中完全落在替换新生内容上
        for c in matches:
            env = coverage.origin_envelope(c.start, c.end)
            if env is None:
                c.generated = True
            else:
                c.source_start, c.source_end = env

        stages.append(
            StageResultInfo(
                index=si,
                input_text=current,
                output_text=masked,
                matches=matches,
                rule_errors=errors,
                applied_count=applied,
                rejected_count=rejected,
                rules_total=len(rules),
            )
        )

        # 为下一阶段构造出处覆盖（变长、删除、重叠与否胜出均在此正确组合）
        coverage = _Coverage(_build_output_coverage(coverage, accepted, len(current)))
        current = masked

        if max_length is not None and len(current) > max_length:
            raise TextTooLongError(si, len(current), max_length)

        rules_total += len(rules)
        rules_invalid += len(errors)
        matches_found += len(matches)
        matches_applied += applied
        matches_rejected += rejected

    return PipelineMaskResult(
        original_text=text,
        final_text=current,
        stages=stages,
        rules_total=rules_total,
        rules_invalid=rules_invalid,
        matches_found=matches_found,
        matches_applied=matches_applied,
        matches_rejected=matches_rejected,
    )
