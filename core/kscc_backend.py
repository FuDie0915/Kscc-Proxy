"""KSCC 后端封装。

在本包内重写 KSCC 认证(不 import 项目的 kscc_client),提供两种访问方式:

- ``anthropic.AsyncAnthropic`` SDK 客户端:给 OpenAI 端点用,因为需要类型化
  事件/响应做 OpenAI↔Anthropic 格式转换。
- ``httpx.AsyncClient``:给 Anthropic 端点用,字节级透传,最保真。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import anthropic
import httpx


# KSCC 代理要求的固定请求头(不含 Authorization,在 __init__ 中拼接)
_KSCC_FIXED_HEADERS: dict[str, str] = {
    "x-ksc-company-code": "seasun",
    "ksyun-code-type": "kscc-cli",
    "ksyun-code-version": "1.1.20",
    "User-Agent": "claude-cli/1.1.20 (external, cli)",
    "Accept": "application/json",
}

# 附加在 messages 端点上的查询参数
_KSCC_EXTRA_QUERY: dict[str, str] = {"beta": "true"}


class KSCCBackend:
    """KSCC 后端访问器,持有 SDK 与 httpx 两个客户端。"""

    def __init__(self, token: str, base_url: str) -> None:
        headers = {"Authorization": f"Bearer {token}", **_KSCC_FIXED_HEADERS}
        self._base = base_url.rstrip("/")
        self._messages_url = f"{self._base}/v1/messages?beta=true"

        # SDK 客户端:api_key="dummy" 占位阻止 SDK 从环境变量读 key,
        # 真实认证走 default_headers 里的 Authorization。
        self._sdk = anthropic.AsyncAnthropic(
            api_key="dummy",
            base_url=base_url,
            default_headers=headers,
        )
        # httpx 客户端:Anthropic 端点字节透传用
        self._http = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    # -- SDK 路径(给 OpenAI 端点) -----------------------------------------

    async def create(self, **kwargs: Any) -> Any:
        """非流式调用 KSCC messages 端点,返回 SDK 类型化 Message。"""
        extra = dict(kwargs.pop("extra_query", {}))
        extra.update(_KSCC_EXTRA_QUERY)
        kwargs["extra_query"] = extra
        kwargs["stream"] = False
        return await self._sdk.messages.create(**kwargs)

    async def create_stream(self, **kwargs: Any) -> Any:
        """流式调用 KSCC messages 端点,返回 SDK AsyncStream(需 async with)。"""
        extra = dict(kwargs.pop("extra_query", {}))
        extra.update(_KSCC_EXTRA_QUERY)
        kwargs["extra_query"] = extra
        kwargs["stream"] = True
        return await self._sdk.messages.create(**kwargs)

    # -- httpx 路径(给 Anthropic 端点透传) ---------------------------------

    async def raw_post(self, payload: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[int, Any]:
        """非流式透传:POST 后端 messages 端点,返回 (status, body_dict)。

        ``headers`` 为客户端需透传的功能性头(如 anthropic-version/beta),
        与固定头 merge 后发送(客户端功能性头优先;Authorization 等认证由固定头覆盖)。
        """
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        resp = await self._http.post(
            self._messages_url,
            json=payload,
            headers=req_headers,
        )
        try:
            body: Any = resp.json()
        except Exception:
            body = {"error": {"type": "bad_response", "message": resp.text}}
        return resp.status_code, body

    async def raw_stream(self, payload: dict[str, Any], headers: dict[str, str] | None = None) -> AsyncIterator[str]:
        """流式字节透传:逐块 yield 后端 SSE 文本。

        ``headers`` 为客户端需透传的功能性头(见 :meth:`raw_post`)。
        后端非 200 时发一个 Anthropic 风格的 ``event: error`` 帧给客户端。
        """
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        async with self._http.stream(
            "POST",
            self._messages_url,
            json=payload,
            headers=req_headers,
            timeout=None,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                try:
                    err = json.loads(body)
                except Exception:
                    err = {
                        "type": "error",
                        "error": {
                            "type": "upstream_error",
                            "message": body.decode("utf-8", "replace"),
                        },
                    }
                yield f"event: error\ndata: {json.dumps(err, ensure_ascii=False)}\n\n"
                return
            async for chunk in resp.aiter_bytes():
                yield chunk.decode("utf-8", "replace")

    async def aclose(self) -> None:
        """关闭底层客户端,优雅退出时调用。"""
        await self._http.aclose()
        await self._sdk.close()
