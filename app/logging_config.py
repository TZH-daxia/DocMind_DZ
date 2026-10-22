"""日志初始化：按天轮转的系统日志 + 可选控制台（仅真实终端）。

参考 DocMind 的做法：
- 按天轮转（midnight）、保留 N 天，避免单个文件无限增长；
- `delay=True`：没有日志就不创建空文件；
- 控制台只在 stdout 是 TTY 时启用，避免 uvicorn --reload 的管道阻塞事件循环；
- 过滤正常关停时的 KeyboardInterrupt / CancelledError 噪音；
- 第三方库（uvicorn / httpx / openai）统一降到 WARNING。
"""

import logging
import logging.config
import sys
from pathlib import Path
from typing import Any

from app.config import Settings

LOG_FILE_NAME = "app.log"

# 不需要 INFO 级别日志的第三方库
NOISY_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "httpx",
    "httpx2",
    "httpcore",
    "openai",
)


def _console_enabled(settings: Settings) -> bool:
    """stdout 为真实终端时才写控制台。

    uvicorn --reload 的 worker 进程 stdout 可能是管道，父进程不及时消费时
    控制台写入会阻塞事件循环（曾导致任务卡住、/health 无响应），
    因此非终端环境只写文件日志。
    """

    if not settings.log_console:
        return False
    try:
        stream = sys.stdout
        return stream is not None and bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


class ShutdownNoiseFilter(logging.Filter):
    """过滤服务正常关停时产生的 KeyboardInterrupt / CancelledError 堆栈噪音。"""

    _NOISE_EXC_NAMES = frozenset({"KeyboardInterrupt", "CancelledError", "SystemExit"})

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR:
            return True
        if (
            record.exc_info is not None
            and record.exc_info[0] is not None
            and record.exc_info[0].__name__ in self._NOISE_EXC_NAMES
        ):
            return False
        message = record.getMessage()
        return "KeyboardInterrupt" not in message and "CancelledError" not in message


def configure_logging(settings: Settings) -> None:
    """配置系统日志：按天轮转、保留 N 天；可选控制台。"""

    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILE_NAME

    root = logging.getLogger()
    if any(getattr(handler, "_docmind_dz", False) for handler in root.handlers):
        return

    level = settings.log_level.upper()
    console = _console_enabled(settings)
    handler_names = ["app_file", *(["console"] if console else [])]

    config: dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "filters": {
            "shutdown_noise": {"()": "app.logging_config.ShutdownNoiseFilter"},
        },
        "handlers": {
            "app_file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "formatter": "standard",
                "filters": ["shutdown_noise"],
                "level": level,
                "filename": str(log_path),
                "when": "midnight",
                "backupCount": max(settings.log_retention_days, 1),
                "encoding": "utf-8",
                "delay": True,
            },
        },
        "loggers": {name: {"level": "WARNING"} for name in NOISY_LOGGERS},
        "root": {"handlers": handler_names, "level": level},
    }
    if console:
        config["handlers"]["console"] = {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["shutdown_noise"],
            "level": level,
            "stream": "ext://sys.stdout",
        }

    logging.config.dictConfig(config)
    # 标记本次创建的 handler，重复调用时直接返回，避免日志重复输出
    for handler in logging.getLogger().handlers:
        handler._docmind_dz = True  # type: ignore[attr-defined]

    logging.getLogger(__name__).info(
        "系统日志已初始化：level=%s retention=%s 天 console=%s file=%s",
        level,
        settings.log_retention_days,
        console,
        log_path,
    )
