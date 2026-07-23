"""Anthropic 兼容端点 /v1/messages(httpx 原始透传)。

后端本就是 Anthropic 格式,此端点零解析透传:仅覆盖 model,并把客户端的
``anthropic-version`` / ``anthropic-beta`` 等功能性请求头一并透传给后端
(认证 Authorization 由固定头覆盖,不透传客户端的 auth)。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..core.config import map_model
from ..core.sse import streaming_response

logger = logging.getLogger("kscc_proxy")

router = APIRouter()

# 客户端头 → 后端:这些功能性头透传(认证头除外,由 backend 固定头覆盖)
_PASSTHROUGH_HEADERS = ("anthropic-version", "anthropic-beta")


def _client_headers(request: Request) -> dict[str, str]:
    """提取需透传的客户端功能性头(小写名)。"""
    out: dict[str, str] = {}
    for name in _PASSTHROUGH_HEADERS:
        val = request.headers.get(name)
        if val:
            out[name] = val
    return out


@router.post("/v1/messages")
async def messages(request: Request) -> Any:
    backend = request.app.state.backend
    config = request.app.state.config
    t0 = time.perf_counter()
    method = request.method
    path = request.url.path

    try:
        payload = await request.json()
    except Exception as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        logger.warning("%s %s 400 %dms invalid_request: %s", method, path, ms, exc)
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": {"type": "invalid_request", "message": str(exc)}},
        )

    if not isinstance(payload, dict):
        ms = int((time.perf_counter() - t0) * 1000)
        logger.warning("%s %s 400 %dms body must be an object", method, path, ms)
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": {"type": "invalid_request", "message": "body must be an object"}},
        )

    # 仅覆盖 model,其余字段原样透传
    model = map_model(payload.get("model"), config)
    payload["model"] = model
    client_headers = _client_headers(request)
    is_stream = bool(payload.get("stream"))

    if is_stream:
        async def _gen():
            async for chunk in backend.raw_stream(payload, headers=client_headers):
                yield chunk
            ms = int((time.perf_counter() - t0) * 1000)
            logger.info("%s %s 200 %dms model=%s stream=true", method, path, ms, model)

        return streaming_response(_gen())

    try:
        status, body = await backend.raw_post(payload, headers=client_headers)
    except httpx.HTTPError as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        logger.warning("%s %s 502 %dms raw_post failed: %s", method, path, ms, exc)
        return JSONResponse(
            status_code=502,
            content={"type": "error", "error": {"type": "upstream_error", "message": str(exc)}},
        )
    ms = int((time.perf_counter() - t0) * 1000)
    logger.info("%s %s %d %dms model=%s stream=false", method, path, status, ms, model)
    return JSONResponse(content=body, status_code=status)
