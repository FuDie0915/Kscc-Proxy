"""请求体模型。

OpenAI 端点用 ``OpenAIChatRequest`` 做宽松校验(extra="allow",未知字段不报错);
Anthropic 端点直接用原始 dict 透传,不做严格校验。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class OpenAIChatRequest(BaseModel):
    """OpenAI /v1/chat/completions 请求体(宽松,保留未知字段)。"""

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[dict[str, Any]]
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    top_p: float | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    stop: str | list[str] | None = None
    stream_options: dict[str, Any] | None = None

    @property
    def effective_max_tokens(self) -> int | None:
        """OpenAI 新版用 max_completion_tokens,旧版用 max_tokens。"""
        return self.max_completion_tokens or self.max_tokens

    @property
    def include_usage(self) -> bool:
        """stream_options.include_usage 是否请求流末 usage。"""
        return bool(self.stream_options and self.stream_options.get("include_usage"))


class OpenAIResponsesRequest(BaseModel):
    """OpenAI /v1/responses 请求体(宽松,保留未知字段)。

    与 Chat Completions 不同:用 ``input`` 而非 ``messages``(``input`` 可为
    字符串或 input item 数组),用 ``instructions`` 表达 system/developer 提示,
    长度上限字段为 ``max_output_tokens``。
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    input: Any  # str 或 input item 数组(message / function_call / function_call_output / reasoning ...)
    instructions: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool | None = None
