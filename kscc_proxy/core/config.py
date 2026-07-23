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
    # 未命中 model_map 的 model 名回退到此值;为空则原样透传(兼容后端真实支持的 model)
    fallback_model: str = ""
    # 运行时字段:启动时从后端 /v1/models 拉取的真实模型 id 集合(不从 JSON 读)。
    # 用于 map_model 判断"客户端发的是后端真实模型还是假名",真实模型原样透传。
    known_models: set[str] = Field(default_factory=set)
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


def mask_base_url(url: str) -> str:
    """脱敏后端地址,用于日志:仅暴露 host 头尾,中间用 ``*`` 遮挡,保留 scheme/path/port。

    host 按点分段,保留首段 + 尾段、中间段用 4 个 ``*`` 代替(露头尾、藏中段):

    例::

        http://120.92.138.34       -> http://120****34
        https://api.kscc.xxx.com   -> https://api****com
        http://1.2.3.4:9000/v1     -> http://1****4:9000/v1

    host 无点(单段)或解析失败时,保留首末各一字符(或过短则原样返回)。
    """
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        if "." in host:
            segs = host.split(".")
            if len(segs) >= 2:
                masked = f"{segs[0]}****{segs[-1]}"
            else:
                masked = host  # 理论不可达
        elif len(host) > 4:
            masked = host[:1] + "****" + host[-1:]
        else:
            return url  # 过短无法有意义地脱敏
        netloc = masked
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        if parts.username:
            creds = parts.username
            if parts.password:
                creds = f"{creds}:{parts.password}"
            netloc = f"{creds}@{netloc}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return url


def map_model(req_model: str | None, config: ProxyConfig) -> str:
    """请求 model 经映射后返回 KSCC 实际 model。

    1. 请求未带 model → 回退 ``default_model``。
    2. 命中 ``model_map`` → 替换(如 ``gpt-4o`` → ``glm-5.2``,处理客户端习惯名)。
    3. 在后端真实模型集合 ``known_models`` 中 → **原样透传**(如 ``kimi-k2.6``
       就用 kimi,实现多模型)。
    4. 都不在(假名,如客户端后台用的 ``gpt-4o-mini``)→ 回退 ``fallback_model``
       (避免后端 403);``fallback_model`` 为空则原样透传。

    ``known_models`` 为空(启动时未拉到后端列表)时,第 3 步视为不命中,
    即未映射的一律走 ``fallback_model`` 兜底 —— 保守,避免 403。
    """
    effective = req_model or config.default_model
    if not effective:
        return effective
    if effective in config.model_map:
        return config.model_map[effective]
    if config.known_models and effective in config.known_models:
        return effective
    if config.fallback_model:
        return config.fallback_model
    return effective


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
