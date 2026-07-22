"""配置加载与校验。

从 JSON 文件读取 ``ProxyConfig``,缺失字段用默认值或环境变量兜底。
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError


class ListenConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787


class AuthConfig(BaseModel):
    # 对外鉴权 API key;为空表示不鉴权
    api_key: str = ""


class DefaultsConfig(BaseModel):
    max_tokens: int = 4096
    temperature: float = 1.0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    # 日志文件路径;为空只输出到 stderr
    file: str = ""


class ProxyConfig(BaseModel):
    kscc_token: str = ""
    kscc_base_url: str = ""
    default_model: str = ""
    model_map: dict[str, str] = Field(default_factory=dict)
    listen: ListenConfig = Field(default_factory=ListenConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth.api_key)


def load_config(path: str | Path) -> ProxyConfig:
    """从 JSON 文件加载配置。

    ``kscc_token`` 为空时回退到环境变量 ``KSCC_AUTH_TOKEN``。
    ``kscc_base_url`` 必填,缺失抛 ``ValueError``。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"配置文件不存在: {p}")

    raw = json.loads(p.read_text(encoding="utf-8"))
    try:
        cfg = ProxyConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"配置文件格式错误: {p}\n{exc}") from exc

    # token 兜底环境变量
    if not cfg.kscc_token:
        cfg = cfg.model_copy(update={"kscc_token": os.environ.get("KSCC_AUTH_TOKEN", "")})

    if not cfg.kscc_token:
        raise ValueError(
            "未配置 kscc_token:请在配置文件设置 kscc_token,或设置环境变量 KSCC_AUTH_TOKEN"
        )
    if not cfg.kscc_base_url:
        raise ValueError("未配置 kscc_base_url")

    return cfg


def map_model(req_model: str | None, config: ProxyConfig) -> str:
    """请求 model 经映射后返回 KSCC 实际 model。

    请求未带 model 时回退 ``default_model``;再按 ``model_map`` 映射;
    命中替换,未命中透传。
    """
    effective = req_model or config.default_model
    if not effective:
        return effective
    return config.model_map.get(effective, effective)


def _prompt_token() -> str:
    """交互式输入 KSCC token,回显隐藏,清洗后非空才返回。

    清洗:去首尾空白、BOM(常见于粘贴)、包裹引号(常见于从配置复制)。
    """
    while True:
        val = getpass.getpass("请输入 KSCC token(输入不可见): ")
        val = _clean_input(val)
        if val:
            return val
        print("token 不能为空,请重新输入。")


def _clean_input(val: str) -> str:
    """清洗粘贴输入:去 BOM、首尾空白、包裹引号。"""
    if not val:
        return ""
    val = val.strip()
    val = val.strip("﻿")  # BOM
    val = val.strip().strip("\"'").strip()  # 包裹引号
    return val


def _prompt_base_url() -> str:
    """交互式输入后端根地址,清洗后容错去掉尾部 ``/`` 与 ``/v1``。"""
    while True:
        val = _clean_input(input(
            "请输入 KSCC 后端根地址(不含 /v1,如 https://api.kscc.xxx.com): "
        ))
        if not val:
            print("base_url 不能为空,请重新输入。")
            continue
        val = val.rstrip("/")
        if val.endswith("/v1"):
            val = val[:-3].rstrip("/")
        return val


def ensure_config(path: str | Path) -> ProxyConfig:
    """加载配置,缺失必填项(或文件不存在)时交互式引导补全并写回。

    只引导 ``kscc_token`` 与 ``kscc_base_url`` 两项;token 有环境变量
    ``KSCC_AUTH_TOKEN`` 兜底时不引导。写回时保留原文件其余字段。
    最终仍委托 :func:`load_config` 完成加载与校验。
    """
    p = Path(path)
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return load_config(path)  # 格式错误,不交互,交给 load_config 报错
        if not isinstance(raw, dict):
            return load_config(path)  # 顶层非 object,同上
    else:
        raw = {}  # 文件不存在 → 全新创建

    need_token = not raw.get("kscc_token") and not os.environ.get("KSCC_AUTH_TOKEN", "")
    need_base = not raw.get("kscc_base_url")

    if need_token or need_base:
        print(f"检测到配置未完成,将引导填写并写回 {p}")
        if need_token:
            raw["kscc_token"] = _prompt_token()
        if need_base:
            raw["kscc_base_url"] = _prompt_base_url()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"已保存配置到 {p}")

    return load_config(path)
