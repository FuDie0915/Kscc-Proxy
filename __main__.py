"""KSCC 中转代理入口。

用法::

    python -m kscc_proxy --config kscc_proxy/config/kscc_proxy.json
    python -m kscc_proxy --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .app import build_app
from .core.config import ensure_config
from .core.logging_setup import setup_logging, uvicorn_log_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kscc_proxy")
    parser.add_argument("--config", default=None, help="配置文件路径(默认:同目录 config/kscc_proxy.json)")
    parser.add_argument("--host", default=None, help="监听地址(覆盖配置)")
    parser.add_argument("--port", type=int, default=None, help="监听端口(覆盖配置)")
    args = parser.parse_args(argv)

    config_path = args.config or str(Path(__file__).parent / "config" / "kscc_proxy.json")
    try:
        config = ensure_config(config_path)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130

    host = args.host or config.listen.host
    port = args.port or config.listen.port

    setup_logging(config.logging.level, config.logging.file)
    logger = logging.getLogger("kscc_proxy")
    logger.info("started %s:%d -> backend %s", host, port, config.kscc_base_url)

    app = build_app(config)
    # 自定义 log_config:复用 root 统一 handler,关掉 uvicorn 默认 access 日志(防重复 + 防方格)
    uvicorn.run(
        app, host=host, port=port,
        log_config=uvicorn_log_config(config.logging.level),
        log_level=config.logging.level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
