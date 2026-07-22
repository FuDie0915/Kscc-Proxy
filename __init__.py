"""KSCC 本地中转代理服务(独立程序)。

零内部依赖:不 import evolve-agent 项目的任何模块。整个目录可拷走单独运行:

    pip install -r requirements.txt
    python -m kscc_proxy --config kscc_proxy.json

对外同时暴露 OpenAI 兼容(/v1/chat/completions)与 Anthropic 兼容(/v1/messages)
两套端点,后端用 KSCC 特殊认证访问金山云 LLM。
"""

__version__ = "0.1.0"
