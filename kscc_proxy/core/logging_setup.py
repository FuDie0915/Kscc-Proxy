"""统一日志配置:一种格式、可选彩色、消除 uvicorn 的方格乱码。

- 所有 logger(kscc_proxy / uvicorn / httpx / anthropic)共用同一 handler 与格式,
  避免原来 kscc_proxy 一行格式 + uvicorn ``INFO:     `` 另一套的割裂。
- 彩色仅在下述条件全部满足时启用,否则纯文本,杜绝 ANSI 码在某些 Windows 终端
  显示成方格:
  * stderr 是真终端(``sys.stderr.isatty()``),重定向到文件自动无色;
  * 调 :func:`colorama.just_fix_windows_console` 让 cmd 经 Win32 API 渲染 ANSI。
- 日志文件始终写无色文本(不混入 ANSI 码)。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

try:
    import colorama

    colorama.just_fix_windows_console()
    _HAS_COLORAMA = True
except Exception:  # colorama 未装也能跑,只是没颜色
    _HAS_COLORAMA = False


# 关键词 → (levelno, ANSI 前缀/后缀)
_LEVEL_COLOR: dict[str, str] = {
    "DEBUG": "\x1b[36m",    # cyan
    "INFO": "\x1b[32m",     # green
    "WARNING": "\x1b[33m",  # yellow
    "ERROR": "\x1b[31m",    # red
    "CRITICAL": "\x1b[35m", # magenta
}
_RESET = "\x1b[0m"

_PLAIN_FMT = "%(asctime)s %(levelname)-5s %(message)s"
_DATEFMT = "%H:%M:%S"


def _want_color() -> bool:
    """仅 stderr 是终端且 colorama 可用时着色;重定向(含日志文件)自动无色。"""
    return _HAS_COLORAMA and sys.stderr.isatty()


class ColorFormatter(logging.Formatter):
    """levelname 染色,其余纯文本;非着色环境退化为普通 Formatter。"""

    def __init__(self, color: bool) -> None:
        super().__init__(fmt=_PLAIN_FMT, datefmt=_DATEFMT)
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if self._color:
            code = _LEVEL_COLOR.get(record.levelname)
            if code:
                msg = f"{code}{msg}{_RESET}"
        return msg


def setup_logging(level: str, file: str) -> None:
    """配置全局日志:统一格式、彩色(终端)/无色(文件)、压制第三方冗余。"""
    levelno = getattr(logging, level.upper(), logging.INFO)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ColorFormatter(_want_color()))

    handlers: list[logging.Handler] = [console]
    if file:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt=_PLAIN_FMT, datefmt=_DATEFMT))
        handlers.append(fh)

    # 统一所有相关 logger 到同一组 handler,禁用 uvicorn 默认的双 handler
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(levelno)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True  # 交由 root 的统一 handler 输出
        lg.setLevel(levelno)
    # uvicorn 默认 access 日志与我们的业务日志重复,且无业务信息,关掉
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    for name in ("httpx", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)


def uvicorn_log_config(level: str) -> dict[str, Any]:
    """给 uvicorn.run(log_config=...) 的 dictConfig:复用 root 统一 handler,
    关掉 uvicorn 默认 access 日志(与我们的业务日志重复且无业务信息),
    让 uvicorn.error 经 root propagate 输出。
    """
    levelno = getattr(logging, level.upper(), logging.INFO)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "loggers": {
            "uvicorn": {"level": levelno, "propagate": True, "handlers": []},
            "uvicorn.error": {"level": levelno, "propagate": True, "handlers": []},
            "uvicorn.access": {"level": logging.WARNING, "propagate": False, "handlers": []},
        },
    }
