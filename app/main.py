"""FastAPI 应用入口：文本发布前脱敏 API。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from . import config
from .masking import (
    RuleInput,
    TextTooLongError,
    mask_pipeline,
    mask_text,
)
from .models import (
    DesensitizeRequest,
    DesensitizeResponse,
    HealthResponse,
    MatchResult,
    OverlappedBy,
    PipelineMatchResult,
    PipelineRequest,
    PipelineResponse,
    PipelineRuleError,
    PipelineStageResult,
    PipelineStats,
    StageStats,
    RuleError,
    Span,
    Stats,
)

app = FastAPI(
    title="文本发布前脱敏 API",
    version="1.0.0",
    description=(
        "提交原文与多条正则脱敏规则（含优先级、替换模板），服务找出全部命中，"
        "按「优先级 → 起始位置 → 规则顺序」确定性消解重叠，仅替换胜出区间，"
        "返回脱敏文本、每个命中的处理状态及原文/结果区间映射。"
    ),
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
        "desensitize_pipeline": "/api/v1/desensitize/pipeline",
    }


@app.post(
    "/api/v1/desensitize",
    response_model=DesensitizeResponse,
    tags=["desensitize"],
    summary="文本脱敏",
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

    rule_inputs = [
        RuleInput(
            index=i,
            rule_id=r.id,
            pattern=r.pattern,
            priority=r.priority,
            replacement=r.replacement,
        )
        for i, r in enumerate(req.rules)
    ]
    result = mask_text(req.text, rule_inputs)

    matches = [
        MatchResult(
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
        for c in result.matches
    ]

    return DesensitizeResponse(
        masked_text=result.masked_text,
        matches=matches,
        rule_errors=[
            RuleError(
                rule_index=e.rule_index,
                rule_id=e.rule_id,
                pattern=e.pattern,
                stage=e.stage,  # type: ignore[arg-type]
                error=e.error,
            )
            for e in result.rule_errors
        ],
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


def _to_pipeline_match(c) -> PipelineMatchResult:
    return PipelineMatchResult(
        rule_index=c.rule_index,
        rule_id=c.rule_id,
        status="applied" if c.applied else "rejected_overlap",
        stage_input_span=Span(start=c.start, end=c.end),
        result_span=Span(start=c.result_start, end=c.result_end),
        source_span=(
            None
            if c.generated
            else Span(start=c.source_start, end=c.source_end)
        ),
        generated=c.generated,
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


@app.post(
    "/api/v1/desensitize/pipeline",
    response_model=PipelineResponse,
    tags=["desensitize"],
    summary="多阶段顺序脱敏",
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
                    f"第 {si} 阶段 rules 数量 {len(stage.rules)} 超过上限 "
                    f"{config.MAX_RULES}"
                ),
            )

    stage_rules = [
        [
            RuleInput(
                index=i,
                rule_id=r.id,
                pattern=r.pattern,
                priority=r.priority,
                replacement=r.replacement,
            )
            for i, r in enumerate(stage.rules)
        ]
        for stage in req.stages
    ]

    try:
        result = mask_pipeline(
            req.text, stage_rules, max_length=config.MAX_TEXT_LENGTH
        )
    except TextTooLongError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"第 {exc.stage_index + 1} 阶段输出长度 {exc.length} 超过上限 "
                f"{exc.limit}"
            ),
        )

    stage_results = [
        PipelineStageResult(
            stage_index=st.index,
            input_text=st.input_text,
            output_text=st.output_text,
            matches=[_to_pipeline_match(c) for c in st.matches],
            rule_errors=[
                PipelineRuleError(
                    rule_index=e.rule_index,
                    rule_id=e.rule_id,
                    pattern=e.pattern,
                    stage=e.stage,  # type: ignore[arg-type]
                    error=e.error,
                    stage_index=e.stage_index,
                )
                for e in st.rule_errors
            ],
            stats=StageStats(
                stage_index=st.index,
                input_length=len(st.input_text),
                output_length=len(st.output_text),
                rules_total=st.rules_total,
                rules_invalid=len(st.rule_errors),
                matches_found=len(st.matches),
                matches_applied=st.applied_count,
                matches_rejected=st.rejected_count,
            ),
        )
        for st in result.stages
    ]

    return PipelineResponse(
        final_text=result.final_text,
        stages=stage_results,
        stats=PipelineStats(
            stages_total=len(result.stages),
            text_length=len(req.text),
            final_length=len(result.final_text),
            rules_total=result.rules_total,
            rules_invalid=result.rules_invalid,
            matches_found=result.matches_found,
            matches_applied=result.matches_applied,
            matches_rejected=result.matches_rejected,
        ),
    )
