"""OpenAI Responses API(/v1/responses)↔ Anthropic 格式转换(纯函数,无 IO)。

Responses 与 Chat Completions 是两套接口:

- 请求用 ``input``(字符串或 input item 数组)而非 ``messages``;``instructions``
  作为 system/developer 提示;长度上限字段为 ``max_output_tokens``。
- 非流式响应用 ``output`` 数组(item 类型:``message`` / ``function_call``)。
- 流式事件为 ``response.*``,每帧带 ``event:`` 前缀,以 ``response.completed`` 收尾。

本模块负责与 Anthropic messages 格式互转,复用 :mod:`convert_openai` 中的纯函数
以保持一致行为。
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from ..core.config import ProxyConfig, map_model
from ..core.models import OpenAIResponsesRequest
from ..core.sse import responses_sse_event

# 复用 chat completions 转换里的纯函数
from .convert_openai import (
    _image_url_to_anthropic,
    _parse_arguments,
    ensure_input_schema,
)


# ---------------------------------------------------------------------------
# Responses input → Anthropic messages + system
# ---------------------------------------------------------------------------


def _append_role(messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    """相邻同 role 合并(Anthropic 要求 user/assistant 严格交替)。"""
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": list(blocks)})


def _content_parts_to_blocks(content: Any) -> list[dict[str, Any]]:
    """Responses message.content(str 或 part 数组)→ Anthropic text/image blocks。

    part 类型:``input_text`` / ``output_text`` → text;``input_image`` → image。
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("input_text", "output_text"):
                text = part.get("text", "")
                if text:
                    blocks.append({"type": "text", "text": text})
            elif ptype == "input_image":
                url = part.get("image_url", "")
                if isinstance(url, dict):
                    url = url.get("url", "")
                img = _image_url_to_anthropic(url or "")
                if img:
                    blocks.append(img)
        return blocks
    return []


def responses_input_to_anthropic(req: OpenAIResponsesRequest) -> tuple[list[dict[str, Any]], str]:
    """Responses ``input``(str | 数组)+ ``instructions`` → (anthropic_messages, system)。

    - ``instructions`` 与 role=system/developer 的消息 → 顶层 system(``\\n\\n`` 合并)。
    - role=user/assistant 消息 → 对应 role 的 content blocks(相邻同 role 合并)。
    - ``function_call`` item → assistant 的 ``tool_use`` block。
    - ``function_call_output`` item → user 的 ``tool_result`` block(连续多条合并)。
    - ``reasoning`` 及未知类型跳过。
    """
    system_parts: list[str] = []
    if req.instructions:
        system_parts.append(req.instructions)

    anthropic_messages: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            anthropic_messages.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    inp = req.input
    if isinstance(inp, str):
        if inp:
            anthropic_messages.append({"role": "user", "content": [{"type": "text", "text": inp}]})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")

            if itype == "message":
                role = item.get("role", "user")
                blocks = _content_parts_to_blocks(item.get("content"))
                if role in ("system", "developer"):
                    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                    if text:
                        system_parts.append(text)
                    continue
                flush_tool_results()
                if not blocks:
                    blocks = [{"type": "text", "text": ""}]
                _append_role(anthropic_messages, "assistant" if role == "assistant" else "user", blocks)

            elif itype == "function_call":
                flush_tool_results()
                _append_role(
                    anthropic_messages,
                    "assistant",
                    [{
                        "type": "tool_use",
                        "id": item.get("call_id") or item.get("id") or "",
                        "name": item.get("name", ""),
                        "input": _parse_arguments(item.get("arguments", "")),
                    }],
                )

            elif itype == "function_call_output":
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": item.get("call_id", ""),
                    "content": item.get("output", ""),
                })
            # reasoning / 其它未知类型:跳过

    flush_tool_results()
    if not anthropic_messages:
        # Anthropic 至少要一条消息;input 为空时补一条空 user
        anthropic_messages.append({"role": "user", "content": [{"type": "text", "text": ""}]})
    return anthropic_messages, "\n\n".join(system_parts)


# ---------------------------------------------------------------------------
# Responses tools / tool_choice → Anthropic
# ---------------------------------------------------------------------------


def responses_tools_to_anthropic(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Responses 工具 → Anthropic 工具。兼容扁平(``name``/``parameters``)与嵌套(``function.*``)两种写法。"""
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type", "function") != "function":
            continue  # 跳过 web_search / file_search / computer_use 等内置工具
        if tool.get("name"):
            name, params, desc = tool["name"], tool.get("parameters", {}), tool.get("description", "")
        else:
            fn = tool.get("function", {}) or {}
            name, params, desc = fn.get("name", ""), fn.get("parameters", {}), fn.get("description", "")
        if not name:
            continue
        out.append({"name": name, "description": desc, "input_schema": ensure_input_schema(params)})
    return out


def responses_tool_choice_to_anthropic(tc: Any) -> dict[str, Any] | None:
    """Responses tool_choice → Anthropic tool_choice(auto/none/required→any/function→tool)。"""
    if tc is None:
        return None
    if isinstance(tc, str):
        return {"type": "any"} if tc == "required" else {"type": tc}
    if isinstance(tc, dict):
        t = tc.get("type")
        if t == "required":
            return {"type": "any"}
        if t == "function":
            return {"type": "tool", "name": tc.get("name", "")}
        return {"type": t or "auto"}
    return None


# ---------------------------------------------------------------------------
# 请求组装
# ---------------------------------------------------------------------------


def responses_to_anthropic_kwargs(req: OpenAIResponsesRequest, config: ProxyConfig) -> dict[str, Any]:
    """Responses 请求 → 可传给 KSCC messages.create 的 kwargs(不含 stream,由调用方加)。

    ``max_tokens`` Anthropic 强制要求,客户端未带则回退 ``defaults.max_tokens``。
    ``temperature`` 客户端未带则**不发**(不注入 ``defaults.temperature``),交由后端
    按各模型自身默认处理 —— 不同后端模型对 temperature 有不同约束(如 kimi-k2.6
    只允许 0.6),注入固定默认会破坏多模型透传。
    """
    messages, system_text = responses_input_to_anthropic(req)
    kwargs: dict[str, Any] = {
        "model": map_model(req.model, config),
        "messages": messages,
        "max_tokens": req.max_output_tokens if req.max_output_tokens is not None else config.defaults.max_tokens,
    }
    if req.temperature is not None:
        kwargs["temperature"] = req.temperature
    if system_text:
        kwargs["system"] = system_text
    tools = responses_tools_to_anthropic(req.tools)
    if tools:
        kwargs["tools"] = tools
    tc = responses_tool_choice_to_anthropic(req.tool_choice)
    if tc is not None:
        kwargs["tool_choice"] = tc
    if req.top_p is not None:
        kwargs["top_p"] = req.top_p
    return kwargs


# ---------------------------------------------------------------------------
# 非流式响应转换:Anthropic Message → Responses response 对象
# ---------------------------------------------------------------------------


def anthropic_to_responses(resp: Any, model: str) -> dict[str, Any]:
    """Anthropic Message → OpenAI Responses ``response`` 对象。

    - text block → 归入一个 ``message`` item 的 ``output_text`` part。
    - tool_use block → 一个 ``function_call`` item(``id``/``call_id`` 取 Anthropic block id)。
    - thinking 等其它 block 跳过。
    """
    rid = getattr(resp, "id", "") or f"resp_{int(time.time())}"
    output: list[dict[str, Any]] = []
    cur_msg: dict[str, Any] | None = None
    msg_idx = 0

    def start_msg() -> dict[str, Any]:
        nonlocal msg_idx
        m = {
            "type": "message",
            "id": f"msg_{rid}_{msg_idx}",
            "status": "completed",
            "role": "assistant",
            "content": [],
        }
        msg_idx += 1
        return m

    for block in getattr(resp, "content", None) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            if cur_msg is None:
                cur_msg = start_msg()
            cur_msg["content"].append({"type": "output_text", "text": getattr(block, "text", "") or ""})
        elif btype == "tool_use":
            if cur_msg is not None:
                output.append(cur_msg)
                cur_msg = None
            output.append({
                "type": "function_call",
                "id": getattr(block, "id", ""),
                "call_id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "arguments": json.dumps(getattr(block, "input", {}) or {}, ensure_ascii=False),
                "status": "completed",
            })
        # thinking / 其它:跳过
    if cur_msg is not None:
        output.append(cur_msg)
    if not output:
        output.append(start_msg())

    usage = getattr(resp, "usage", None)
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    return {
        "id": rid,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out},
    }


# ---------------------------------------------------------------------------
# 流式:Anthropic 事件 → Responses SSE(response.* 系列)
# ---------------------------------------------------------------------------


async def responses_stream_generator(
    sdk_stream: Any,
    model: str,
) -> AsyncIterator[str]:
    """消费 anthropic SDK 流,逐事件转 OpenAI Responses SSE 字符串行。

    每个 Anthropic content block 映射为一个 Responses output item:文本块 →
    ``message`` item(单个 ``output_text`` part);tool_use 块 → ``function_call`` item。
    thinking 块静默丢弃(与 chat completions 端点行为一致)。
    """
    created = int(time.time())
    response_id = f"resp_{created}"
    output_index = 0
    # Anthropic block index → item 状态
    blocks: dict[Any, dict[str, Any]] = {}
    # out_idx → 最终 item(用于 response.completed)
    items_by_out_idx: dict[int, dict[str, Any]] = {}
    input_tokens = 0
    output_tokens = 0

    def resp_skeleton(status: str, output: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": status,
            "model": model,
            "output": output if output is not None else [],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        }

    def emit(etype: str, payload: dict[str, Any]) -> str:
        return responses_sse_event(etype, payload)

    async with sdk_stream as stream:
        async for event in stream:
            etype = getattr(event, "type", None)

            if etype == "message_start":
                msg = getattr(event, "message", None)
                if msg is not None:
                    mid = getattr(msg, "id", None)
                    if mid:
                        response_id = mid
                    usage = getattr(msg, "usage", None)
                    if usage is not None:
                        input_tokens = getattr(usage, "input_tokens", 0) or 0
                yield emit("response.created", {"type": "response.created", "response": resp_skeleton("in_progress")})
                yield emit("response.in_progress", {"type": "response.in_progress", "response": resp_skeleton("in_progress")})

            elif etype == "content_block_start":
                block = getattr(event, "content_block", None)
                idx = getattr(event, "index", None)
                btype = getattr(block, "type", None) if block else None
                if idx is None:
                    continue
                out_idx = output_index
                output_index += 1
                if btype == "text":
                    item_id = f"msg_{response_id}_{out_idx}"
                    blocks[idx] = {"kind": "text", "out_idx": out_idx, "item_id": item_id, "text": ""}
                    yield emit("response.output_item.added", {
                        "type": "response.output_item.added", "output_index": out_idx,
                        "item": {"type": "message", "id": item_id, "status": "in_progress", "role": "assistant", "content": []},
                    })
                    yield emit("response.content_part.added", {
                        "type": "response.content_part.added", "output_index": out_idx,
                        "content_index": 0, "item_id": item_id,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    })
                elif btype == "tool_use" and block is not None:
                    item_id = getattr(block, "id", "") or f"fc_{response_id}_{out_idx}"
                    name = getattr(block, "name", "")
                    blocks[idx] = {
                        "kind": "tool", "out_idx": out_idx, "item_id": item_id,
                        "name": name, "args": "", "seen": False,
                        "final_input": getattr(block, "input", None),
                    }
                    yield emit("response.output_item.added", {
                        "type": "response.output_item.added", "output_index": out_idx,
                        "item": {"type": "function_call", "id": item_id, "call_id": item_id,
                                 "name": name, "arguments": "", "status": "in_progress"},
                    })
                # thinking / 其它:不发 output item

            elif etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                idx = getattr(event, "index", None)
                dtype = getattr(delta, "type", None) if delta else None
                st = blocks.get(idx) if idx is not None else None
                if st is None:
                    continue
                if dtype == "text_delta" and st["kind"] == "text":
                    text = getattr(delta, "text", "") or ""
                    if text:
                        st["text"] += text
                        yield emit("response.output_text.delta", {
                            "type": "response.output_text.delta", "output_index": st["out_idx"],
                            "content_index": 0, "item_id": st["item_id"], "delta": text,
                        })
                elif dtype == "input_json_delta" and st["kind"] == "tool":
                    partial = getattr(delta, "partial_json", "") or ""
                    if partial:
                        st["args"] += partial
                        st["seen"] = True
                        yield emit("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta", "output_index": st["out_idx"],
                            "item_id": st["item_id"], "delta": partial,
                        })
                # thinking_delta 丢弃

            elif etype == "content_block_stop":
                idx = getattr(event, "index", None)
                st = blocks.pop(idx, None) if idx is not None else None
                if st is None:
                    continue
                out_idx = st["out_idx"]
                if st["kind"] == "text":
                    text = st["text"]
                    yield emit("response.output_text.done", {
                        "type": "response.output_text.done", "output_index": out_idx,
                        "content_index": 0, "item_id": st["item_id"], "text": text,
                    })
                    yield emit("response.content_part.done", {
                        "type": "response.content_part.done", "output_index": out_idx,
                        "content_index": 0, "item_id": st["item_id"],
                        "part": {"type": "output_text", "text": text, "annotations": []},
                    })
                    item = {
                        "type": "message", "id": st["item_id"], "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                    items_by_out_idx[out_idx] = item
                    yield emit("response.output_item.done", {
                        "type": "response.output_item.done", "output_index": out_idx, "item": item,
                    })
                else:  # tool_use
                    # 后端未走 input_json_delta 时,用 content_block 的完整 input 兜底
                    if not st["seen"] and st.get("final_input") is not None:
                        args = json.dumps(st["final_input"], ensure_ascii=False)
                        st["args"] = args
                        yield emit("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta", "output_index": out_idx,
                            "item_id": st["item_id"], "delta": args,
                        })
                    args = st["args"]
                    yield emit("response.function_call_arguments.done", {
                        "type": "response.function_call_arguments.done", "output_index": out_idx,
                        "item_id": st["item_id"], "arguments": args,
                    })
                    item = {
                        "type": "function_call", "id": st["item_id"], "call_id": st["item_id"],
                        "name": st["name"], "arguments": args, "status": "completed",
                    }
                    items_by_out_idx[out_idx] = item
                    yield emit("response.output_item.done", {
                        "type": "response.output_item.done", "output_index": out_idx, "item": item,
                    })

            elif etype == "message_delta":
                usage = getattr(event, "usage", None)
                if usage is not None:
                    output_tokens = getattr(usage, "output_tokens", 0) or 0
                    it = getattr(usage, "input_tokens", 0) or 0
                    if it:
                        # 部分后端(KSCC)在 message_start 不回填 input_tokens,而在
                        # message_delta 才给完整 usage,这里兜底补上
                        input_tokens = it

            elif etype == "message_stop":
                output = [items_by_out_idx[k] for k in sorted(items_by_out_idx)]
                yield emit("response.completed", {
                    "type": "response.completed", "response": resp_skeleton("completed", output),
                })
