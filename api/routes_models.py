"""OpenAI / Anthropic 兼容的模型列表端点 GET /v1/models。

透传后端 KSCC 的 /v1/models(标准 OpenAI 格式),让客户端在 UI 里发现并
选择真实可用的模型(如 glm-5.2 / kimi-k2.6 / mimo-v2.5 等)。选中的真实
模型名经 map_model 原样透传给后端(见 core.config.map_model)。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("kscc_proxy")

router = APIRouter()


@router.get("/v1/models")
@router.get("/v1/models/")
async def list_models(request: Request) -> Any:
    backend = request.app.state.backend
    t0 = time.perf_counter()
    method = request.method
    path = request.url.path

    try:
        status, body = await backend.list_models()
    except httpx.HTTPError as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        logger.warning("%s %s 502 %dms list_models failed: %s", method, path, ms, exc)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "upstream_error"}},
        )

    ms = int((time.perf_counter() - t0) * 1000)
    count = 0
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        count = len(body["data"])
    logger.info("%s %s %d %dms models=%d", method, path, status, ms, count)
    return JSONResponse(content=body, status_code=status)
