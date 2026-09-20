"""FastAPI 应用入口：文本发布前脱敏 API。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from . import config
from .masking import RuleInput, mask_text
from .models import (
    DesensitizeRequest,
    DesensitizeResponse,
    HealthResponse,
    MatchResult,
    OverlappedBy,
    PipelineMatchResult,
    PipelineRequest,
    PipelineResponse,
    PipelineStats,
    RuleError,
    Span,
    StageResult,
    Stats,
)
from .pipeline import run_pipeline

app = FastAPI(
    title="文本发布前脱敏 API",
    version="1.1.0",
    description=(
        "提交原文与多条正则脱敏规则（含优先级、替换模板），服务找出全部命中，"
        "按「优先级 → 起始位置 → 规则顺序」确定性消解重叠，仅替换胜出区间，"
        "返回脱敏文本、每个命中的处理状态及原文/结果区间映射。"
        "另提供多阶段流水线接口，后续阶段可命中前序替换生成的内容，"
        "并把每个命中映射回初始原文。"
    ),
)


def _rule_inputs(rules) -> list:
    return [
        RuleInput(
            index=i,
            rule_id=r.id,
            pattern=r.pattern,
            priority=r.priority,
            replacement=r.replacement,
        )
        for i, r in enumerate(rules)
    ]


def _match_result(c) -> MatchResult:
    return MatchResult(
        rule_index=c.rule_index,
        rule_id=c.rule_id,
        status="applied" if c.applied else "rejected_overlap",
        original_span=Span(start=c.start, end=c.end),
        result_span=Span(start=c.result_start, end=c.result_end),
        matched_text=c.matched_text,
        replacement=c.replacement if c.applied else None,
        reason=(
            None
            if c.applied
            else (
                f"与胜出区间 [{c.winner.start}, {c.winner.end})"
                f"（规则 #{c.winner.rule_index}）重叠，被否决"
            )
        ),
        overlapped_by=(
            None
            if c.applied
            else OverlappedBy(
                rule_index=c.winner.rule_index,
                rule_id=c.winner.rule_id,
                span=Span(start=c.winner.start, end=c.winner.end),
            )
        ),
    )


def _rule_error(e) -> RuleError:
    return RuleError(
        rule_index=e.rule_index,
        rule_id=e.rule_id,
        pattern=e.pattern,
        stage=e.stage,  # type: ignore[arg-type]
        error=e.error,
    )


@app.get("/health", response_model=HealthResponse, tags=["meta"], summary="健康检查")
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/", tags=["meta"], summary="服务信息")
def root() -> dict:
    return {
        "service": "text-desensitize-api",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
        "desensitize": "/api/v1/desensitize",
        "pipeline": "/api/v1/desensitize/pipeline",
    }


@app.post(
    "/api/v1/desensitize",
    response_model=DesensitizeResponse,
    tags=["desensitize"],
    summary="文本脱敏（单阶段）",
)
def desensitize(req: DesensitizeRequest) -> DesensitizeResponse:
    if len(req.text) > config.MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"text 长度 {len(req.text)} 超过上限 {config.MAX_TEXT_LENGTH}",
        )
    if len(req.rules) > config.MAX_RULES:
        raise HTTPException(
            status_code=422,
            detail=f"rules 数量 {len(req.rules)} 超过上限 {config.MAX_RULES}",
        )

    # 与单阶段端点共用同一套核心消解逻辑
    result = mask_text(req.text, _rule_inputs(req.rules))

    return DesensitizeResponse(
        masked_text=result.masked_text,
        matches=[_match_result(c) for c in result.matches],
        rule_errors=[_rule_error(e) for e in result.rule_errors],
        stats=Stats(
            text_length=len(req.text),
            masked_length=len(result.masked_text),
            rules_total=len(req.rules),
            rules_invalid=len(result.rule_errors),
            matches_found=len(result.matches),
            matches_applied=result.applied_count,
            matches_rejected=result.rejected_count,
        ),
    )


@app.post(
    "/api/v1/desensitize/pipeline",
    response_model=PipelineResponse,
    tags=["desensitize"],
    summary="文本脱敏（多阶段流水线）",
)
def desensitize_pipeline(req: PipelineRequest) -> PipelineResponse:
    if len(req.text) > config.MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"text 长度 {len(req.text)} 超过上限 {config.MAX_TEXT_LENGTH}",
        )
    if len(req.stages) > config.MAX_STAGES:
        raise HTTPException(
            status_code=422,
            detail=f"stages 数量 {len(req.stages)} 超过上限 {config.MAX_STAGES}",
        )
    for si, stage in enumerate(req.stages):
        if len(stage.rules) > config.MAX_RULES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"阶段 #{si} rules 数量 {len(stage.rules)} "
                    f"超过上限 {config.MAX_RULES}"
                ),
            )

    pipeline = run_pipeline(
        req.text, [_rule_inputs(stage.rules) for stage in req.stages]
    )

    stage_responses: list = []
    rules_total = 0
    rules_invalid = 0
    matches_found = 0
    matches_applied = 0
    matches_rejected = 0

    for st in pipeline.stages:
        match_responses = [
            PipelineMatchResult(
                stage_index=m.stage_index,
                rule_index=m.rule_index,
                rule_id=m.rule_id,
                status=m.status,
                input_span=Span(start=m.input_span[0], end=m.input_span[1]),
                result_span=Span(start=m.result_span[0], end=m.result_span[1]),
                source_span=(
                    None
                    if m.source_span is None
                    else Span(start=m.source_span[0], end=m.source_span[1])
                ),
                generated=m.generated,
                matched_text=m.matched_text,
                replacement=m.replacement,
                reason=m.reason,
                overlapped_by=(
                    None
                    if m.overlapped_by is None
                    else OverlappedBy(
                        rule_index=m.overlapped_by["rule_index"],
                        rule_id=m.overlapped_by["rule_id"],
                        span=Span(
                            start=m.overlapped_by["span"][0],
                            end=m.overlapped_by["span"][1],
                        ),
                    )
                ),
            )
            for m in st.matches
        ]
        stage_responses.append(
            StageResult(
                stage_index=st.stage_index,
                masked_text=st.masked_text,
                matches=match_responses,
                rule_errors=[_rule_error(e) for e in st.rule_errors],
                stats=Stats(
                    text_length=st.input_length,
                    masked_length=len(st.masked_text),
                    rules_total=st.rules_total,
                    rules_invalid=len(st.rule_errors),
                    matches_found=len(st.matches),
                    matches_applied=st.applied_count,
                    matches_rejected=st.rejected_count,
                ),
            )
        )
        rules_total += st.rules_total
        rules_invalid += len(st.rule_errors)
        matches_found += len(st.matches)
        matches_applied += st.applied_count
        matches_rejected += st.rejected_count

    return PipelineResponse(
        masked_text=pipeline.masked_text,
        stages=stage_responses,
        stats=PipelineStats(
            text_length=len(req.text),
            masked_length=len(pipeline.masked_text),
            stages_total=len(req.stages),
            rules_total=rules_total,
            rules_invalid=rules_invalid,
            matches_found=matches_found,
            matches_applied=matches_applied,
            matches_rejected=matches_rejected,
        ),
    )
