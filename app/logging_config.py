"""日志初始化：默认只写滚动文件，避免阻塞事件循环。"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import Settings


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(handler, "_docmind_dz", False) for handler in root.handlers):
        return

    root.setLevel(settings.log_level.upper())
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        Path(settings.log_dir) / "app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler._docmind_dz = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    if settings.log_console:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        console._docmind_dz = True  # type: ignore[attr-defined]
        root.addHandler(console)
