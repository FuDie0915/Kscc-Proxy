"""OpenAI 兼容端点 /v1/chat/completions(非流式 + 流式)。"""

from __future__ import annotations

import logging
import time
from typing import Any

import anthropic
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .convert_openai import (
    anthropic_response_to_openai,
    openai_stream_generator,
    openai_to_anthropic_kwargs,
)
from ..core.models import OpenAIChatRequest
from ..core.sse import streaming_response

logger = logging.getLogger("kscc_proxy")

router = APIRouter()


def _error_response(exc: Exception) -> JSONResponse:
    """把 anthropic/httpx 异常转成 OpenAI 风格错误响应。"""
    if isinstance(exc, anthropic.APIStatusError):
        return JSONResponse(
            status_code=getattr(exc, "status_code", 500),
            content={"error": {"message": str(exc), "type": type(exc).__name__}},
        )
    return JSONResponse(status_code=502, content={"error": {"message": str(exc)}})


def _log_request(method: str, path: str, status: int, ms: float, model: str, stream: bool, usage: str = "") -> None:
    """打一行请求日志(简洁彩色风格)。"""
    extra = f" model={model} stream={'true' if stream else 'false'}"
    if usage:
        extra += f" {usage}"
    logger.info("%s %s %d %dms%s", method, path, status, ms, extra)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    backend = request.app.state.backend
    config = request.app.state.config
    t0 = time.perf_counter()
    method = request.method
    path = request.url.path

    try:
        raw = await request.json()
        req = OpenAIChatRequest.model_validate(raw)
    except Exception as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        logger.warning("%s %s 400 %dms invalid_request: %s", method, path, ms, exc)
        raise HTTPException(status_code=400, detail=f"invalid request: {exc}") from exc

    kwargs = openai_to_anthropic_kwargs(req, config)
    model = kwargs["model"]

    if req.stream:
        try:
            stream = await backend.create_stream(**kwargs)
        except Exception as exc:
            ms = int((time.perf_counter() - t0) * 1000)
            logger.warning("%s %s 502 %dms create_stream failed: %s", method, path, ms, exc)
            return _error_response(exc)
        # 流式耗时在生成器结束时记录
        async def _gen():
            async for chunk in openai_stream_generator(stream, model, req.include_usage):
                yield chunk
            ms = int((time.perf_counter() - t0) * 1000)
            _log_request(method, path, 200, ms, model, True)

        return streaming_response(_gen())

    try:
        resp = await backend.create(**kwargs)
    except Exception as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        logger.warning("%s %s 502 %dms create failed: %s", method, path, ms, exc)
        return _error_response(exc)
    ms = int((time.perf_counter() - t0) * 1000)
    usage = getattr(resp, "usage", None)
    usage_str = ""
    if usage is not None:
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        usage_str = f"tok={inp}/{out}"
    _log_request(method, path, 200, ms, model, False, usage_str)
    return JSONResponse(content=anthropic_response_to_openai(resp, model))
