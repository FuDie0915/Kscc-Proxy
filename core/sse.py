"""SSE 行格式化与 StreamingResponse 包装。"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi.responses import StreamingResponse


def openai_sse_chunk(payload: dict[str, Any]) -> str:
    """OpenAI 风格 SSE:data: {json}\\n\\n(无 event: 前缀)。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def openai_sse_done() -> str:
    """OpenAI 流终止标记。"""
    return "data: [DONE]\n\n"


def responses_sse_event(event_type: str, payload: dict[str, Any]) -> str:
    """OpenAI Responses 风格 SSE:event: <type>\\ndata: {json}\\n\\n。

    Responses 流式与 Chat Completions 不同:每帧带 ``event:`` 前缀,且不以
    ``data: [DONE]`` 结尾(由 ``response.completed`` 收尾)。
    """
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def streaming_response(
    generator: AsyncIterator[str],
    media_type: str = "text/event-stream",
) -> StreamingResponse:
    """把 async 字符串生成器包成 SSE StreamingResponse。"""
    return StreamingResponse(
        generator,
        media_type=media_type,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 防 nginx 缓冲
        },
    )
