"""FastAPI app 组装:挂载路由、可选鉴权、健康检查、生命周期。"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .core.config import ProxyConfig
from .core.kscc_backend import KSCCBackend
from .api.routes_anthropic import router as anthropic_router
from .api.routes_openai import router as openai_router
from .api.routes_responses import router as responses_router
from .api.routes_models import router as models_router

logger = logging.getLogger("kscc_proxy")


def build_app(config: ProxyConfig) -> FastAPI:
    """构造 FastAPI app,挂载 OpenAI 与 Anthropic 两套端点。"""
    backend = KSCCBackend(config.kscc_token, config.kscc_base_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.backend = backend
        app.state.config = config
        # 拉取后端模型列表,缓存真实模型 id 供 map_model 判断(真实模型原样透传,
        # 未映射的假名才走 fallback_model 兜底)。失败则留空,保守退化为兜底。
        try:
            status, body = await backend.list_models()
            if status == 200 and isinstance(body, dict):
                ids = {
                    m.get("id")
                    for m in body.get("data", [])
                    if isinstance(m, dict) and m.get("id")
                }
                if ids:
                    config.known_models = ids
                    logger.info("loaded %d backend models: %s", len(ids), ", ".join(sorted(ids)))
        except Exception as exc:  # noqa: BLE001 - 启动期拉取失败不应阻断服务
            logger.warning("failed to load backend models (map_model 将全部走 fallback): %s", exc)
        yield
        await backend.aclose()

    app = FastAPI(title="KSCC Proxy", version="0.1.0", lifespan=lifespan)

    if config.auth_enabled:
        @app.middleware("http")
        async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
            if request.url.path.rstrip("/") == "/healthz":
                return await call_next(request)
            token = _extract_token(request)
            if not token or not secrets.compare_digest(token, config.auth.api_key):
                return JSONResponse(
                    status_code=401,
                    content={"error": {"message": "invalid or missing api key"}},
                )
            return await call_next(request)

    @app.get("/healthz")
    @app.get("/healthz/")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(openai_router)
    app.include_router(responses_router)
    app.include_router(models_router)
    app.include_router(anthropic_router)

    return app


def _extract_token(request: Request) -> str:
    """从 Authorization(Bearer 或裸)或 x-api-key 头取 token。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth:
        return auth.strip()
    return request.headers.get("x-api-key", "").strip()
