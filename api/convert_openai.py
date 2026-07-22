"""OpenAI ↔ Anthropic 格式转换(纯函数,无 IO)。

包含:messages 互转、tools 转换、非流式响应转换、流式事件 → OpenAI SSE chunk 映射。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, AsyncIterator

from ..core.config import ProxyConfig, map_model
from ..core.models import OpenAIChatRequest
from ..core.sse import openai_sse_chunk, openai_sse_done


# ---------------------------------------------------------------------------
# stop_reason / finish_reason 映射
# ---------------------------------------------------------------------------


def map_stop_reason(stop_reason: Any) -> str:
    """Anthropic stop_reason → OpenAI finish_reason。"""
    if stop_reason == "end_turn":
        return "stop"
    if stop_reason == "tool_use":
        return "tool_calls"
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason == "stop_sequence":
        return "stop"
    return stop_reason or "stop"


def _usage_to_openai(usage: Any) -> dict[str, int]:
    """Anthropic usage 对象 → OpenAI usage dict。"""
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    return {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}


# ---------------------------------------------------------------------------
# OpenAI messages → Anthropic messages + system
# ---------------------------------------------------------------------------


def _extract_text(content: Any) -> str:
    """从 OpenAI content(str 或 text 块数组)拼接纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _image_url_to_anthropic(url: str) -> dict[str, Any] | None:
    """OpenAI image_url → Anthropic image block(data URL 解析 base64,普通 URL 用 url source)。"""
    if not url:
        return None
    if url.startswith("data:"):
        match = re.match(r"data:([^;]+);base64,(.+)", url)
        if match:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": match.group(1),
                    "data": match.group(2),
                },
            }
        return None
    return {"type": "image", "source": {"type": "url", "url": url}}


def _content_to_blocks(content: Any) -> list[dict[str, Any]]:
    """OpenAI content(str 或 list)→ Anthropic text/image blocks。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                text = part.get("text", "")
                if text:
                    blocks.append({"type": "text", "text": text})
            elif ptype == "image_url":
                url = part.get("image_url", {})
                url = url.get("url", "") if isinstance(url, dict) else ""
                img = _image_url_to_anthropic(url)
                if img:
                    blocks.append(img)
        return blocks
    return []


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """OpenAI tool arguments(JSON 字符串)→ dict,容错返回 {}。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
        except Exception:
            return {}
    return {}


def _tool_result_content(content: Any) -> Any:
    """OpenAI tool 消息 content → Anthropic tool_result content(str 或 blocks)。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = _content_to_blocks(content)
        return blocks if blocks else ""
    if content is None:
        return ""
    return str(content)


def convert_messages(
    openai_messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """OpenAI messages → (anthropic_messages, system_text)。

    - role=system:提取到顶层 system,多条用 ``\\n\\n`` 合并。
    - role=tool:转 tool_result block,连续 tool 合并进同一条 user 消息。
    - role=assistant:content 文本 + tool_calls(tool_use block)。
    - role=user:文本/图片 blocks。
    """
    anthropic_messages: list[dict[str, Any]] = []
    system_parts: list[str] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            anthropic_messages.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for msg in openai_messages:
        role = msg.get("role")

        if role == "system":
            text = _extract_text(msg.get("content"))
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": _tool_result_content(msg.get("content")),
                }
            )
            continue

        # user / assistant:先 flush 累积的 tool_result
        flush_tool_results()

        content_blocks = _content_to_blocks(msg.get("content"))

        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": _parse_arguments(fn.get("arguments", "")),
                    }
                )
            if not content_blocks:
                content_blocks = [{"type": "text", "text": ""}]
            # 相邻同 role 合并(Anthropic 要求 user/assistant 严格交替)
            if anthropic_messages and anthropic_messages[-1]["role"] == "assistant":
                anthropic_messages[-1]["content"].extend(content_blocks)
            else:
                anthropic_messages.append({"role": "assistant", "content": content_blocks})
        else:  # user(及其它未知 role 按 user 处理)
            if not content_blocks:
                content_blocks = [{"type": "text", "text": ""}]
            if anthropic_messages and anthropic_messages[-1]["role"] == "user":
                anthropic_messages[-1]["content"].extend(content_blocks)
            else:
                anthropic_messages.append({"role": "user", "content": content_blocks})

    flush_tool_results()
    return anthropic_messages, "\n\n".join(system_parts)


# ---------------------------------------------------------------------------
# OpenAI tools → Anthropic tools
# ---------------------------------------------------------------------------


def ensure_input_schema(parameters: Any) -> dict[str, Any]:
    """确保 Anthropic input_schema 顶层为 JSON Schema object。"""
    if not isinstance(parameters, dict):
        return {"type": "object"}
    schema = dict(parameters)
    if schema.get("type") is None:
        schema["type"] = "object"
    return schema


def openai_tools_to_anthropic(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """OpenAI 工具 schema → Anthropic 工具 schema。"""
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if tool.get("type") != "function":
            continue
        fn = tool.get("function", {}) or {}
        name = fn.get("name", "")
        if not name:
            continue
        out.append(
            {
                "name": name,
                "description": fn.get("description", ""),
                "input_schema": ensure_input_schema(fn.get("parameters", {})),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 请求组装
# ---------------------------------------------------------------------------


def openai_to_anthropic_kwargs(req: OpenAIChatRequest, config: ProxyConfig) -> dict[str, Any]:
    """OpenAI 请求 → 可传给 KSCC messages.create 的 kwargs(不含 stream,由调用方加)。

    ``max_tokens`` Anthropic 强制要求,客户端未带则回退 ``defaults.max_tokens``。
    ``temperature`` 客户端未带则**不发**(不注入 ``defaults.temperature``),交由后端
    按各模型自身默认处理 —— 不同后端模型对 temperature 有不同约束(如 kimi-k2.6
    只允许 0.6),注入固定默认会破坏多模型透传。
    """
    messages, system_text = convert_messages(req.messages)
    max_tok = req.effective_max_tokens
    kwargs: dict[str, Any] = {
        "model": map_model(req.model, config),
        "messages": messages,
        "max_tokens": max_tok if max_tok is not None else config.defaults.max_tokens,
    }
    if req.temperature is not None:
        kwargs["temperature"] = req.temperature
    if system_text:
        kwargs["system"] = system_text
    tools = openai_tools_to_anthropic(req.tools)
    if tools:
        kwargs["tools"] = tools
    if req.top_p is not None:
        kwargs["top_p"] = req.top_p
    if req.stop:
        kwargs["stop_sequences"] = req.stop if isinstance(req.stop, list) else [req.stop]
    return kwargs


# ---------------------------------------------------------------------------
# 非流式响应转换
# ---------------------------------------------------------------------------


def anthropic_response_to_openai(resp: Any, model: str) -> dict[str, Any]:
    """Anthropic Message → OpenAI ChatCompletion JSON。"""
    content_text = ""
    tool_calls: list[dict[str, Any]] = []

    for block in getattr(resp, "content", None) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            content_text += getattr(block, "text", "") or ""
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": getattr(block, "id", ""),
                    "type": "function",
                    "function": {
                        "name": getattr(block, "name", ""),
                        "arguments": json.dumps(
                            getattr(block, "input", {}) or {}, ensure_ascii=False
                        ),
                    },
                }
            )

    message: dict[str, Any] = {"role": "assistant", "content": content_text if content_text else None}
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": getattr(resp, "id", "") or f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": map_stop_reason(getattr(resp, "stop_reason", None)),
            }
        ],
        "usage": _usage_to_openai(getattr(resp, "usage", None)),
    }


# ---------------------------------------------------------------------------
# 流式:Anthropic 事件 → OpenAI SSE chunk
# ---------------------------------------------------------------------------


async def openai_stream_generator(
    sdk_stream: Any,
    model: str,
    include_usage: bool,
) -> AsyncIterator[str]:
    """消费 anthropic SDK 流,逐事件转 OpenAI SSE 字符串行。"""
    chat_id = f"chatcmpl-{int(time.time())}"
    created = int(time.time())
    tool_seq = 0
    block_index_to_tool_seq: dict[Any, int] = {}
    block_index_to_type: dict[Any, str] = {}
    tool_input_seen: dict[int, bool] = {}  # tool 序号 → 是否已发过 partial 增量
    input_tokens = 0
    output_tokens = 0
    finish_reason: str | None = None

    def base_chunk(delta: dict[str, Any], fr: str | None = None) -> dict[str, Any]:
        return {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": fr}],
        }

    async with sdk_stream as stream:
        async for event in stream:
            etype = getattr(event, "type", None)

            if etype == "message_start":
                msg = getattr(event, "message", None)
                if msg is not None:
                    mid = getattr(msg, "id", None)
                    if mid:
                        chat_id = mid
                    usage = getattr(msg, "usage", None)
                    if usage is not None:
                        input_tokens = getattr(usage, "input_tokens", 0) or 0
                yield openai_sse_chunk(base_chunk({"role": "assistant", "content": ""}))

            elif etype == "content_block_start":
                block = getattr(event, "content_block", None)
                idx = getattr(event, "index", None)
                btype = getattr(block, "type", None) if block else None
                if idx is not None:
                    block_index_to_type[idx] = btype or ""
                if btype == "tool_use" and block is not None:
                    seq = tool_seq
                    tool_seq += 1
                    if idx is not None:
                        block_index_to_tool_seq[idx] = seq
                    tool_input_seen[seq] = False
                    yield openai_sse_chunk(
                        base_chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": seq,
                                        "id": getattr(block, "id", ""),
                                        "type": "function",
                                        "function": {
                                            "name": getattr(block, "name", ""),
                                            "arguments": "",
                                        },
                                    }
                                ]
                            }
                        )
                    )

            elif etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                idx = getattr(event, "index", None)
                dtype = getattr(delta, "type", None) if delta else None
                if dtype == "text_delta":
                    text = getattr(delta, "text", "") or ""
                    if text:
                        yield openai_sse_chunk(base_chunk({"content": text}))
                elif dtype == "input_json_delta":
                    partial = getattr(delta, "partial_json", "") or ""
                    seq = block_index_to_tool_seq.get(idx) if idx is not None else None
                    if seq is not None and partial:
                        tool_input_seen[seq] = True
                        yield openai_sse_chunk(
                            base_chunk(
                                {"tool_calls": [{"index": seq, "function": {"arguments": partial}}]}
                            )
                        )
                # thinking_delta 默认丢弃

            elif etype == "content_block_stop":
                idx = getattr(event, "index", None)
                if idx is not None and block_index_to_type.get(idx) == "tool_use":
                    seq = block_index_to_tool_seq.get(idx)
                    # 若全程没有 partial_json,在此用完整 input 一次性发(兼容部分后端)
                    if seq is not None and not tool_input_seen.get(seq):
                        block = getattr(event, "content_block", None)
                        final_input = getattr(block, "input", None) if block else None
                        if final_input is not None:
                            args = json.dumps(final_input, ensure_ascii=False)
                            yield openai_sse_chunk(
                                base_chunk(
                                    {"tool_calls": [{"index": seq, "function": {"arguments": args}}]}
                                )
                            )

            elif etype == "message_delta":
                delta = getattr(event, "delta", None)
                stop = getattr(delta, "stop_reason", None) if delta else None
                finish_reason = map_stop_reason(stop) if stop else "stop"
                usage = getattr(event, "usage", None)
                if usage is not None:
                    output_tokens = getattr(usage, "output_tokens", 0) or 0
                yield openai_sse_chunk(base_chunk({}, finish_reason))

            elif etype == "message_stop":
                pass

    # 后端未发 message_delta(异常流)时补一帧 finish_reason,避免客户端等不到结束
    if finish_reason is None:
        finish_reason = "stop"
        yield openai_sse_chunk(base_chunk({}, finish_reason))

    if include_usage:
        yield openai_sse_chunk(
            {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            }
        )
    yield openai_sse_done()
