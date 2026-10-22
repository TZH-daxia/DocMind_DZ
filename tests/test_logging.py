"""系统日志：按天轮转、保留天数、关停噪音过滤。"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.config import Settings
from app.logging_config import ShutdownNoiseFilter, configure_logging


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(log_dir=tmp_path / "logs", log_console=False, **overrides)


def _file_handlers() -> list[TimedRotatingFileHandler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, TimedRotatingFileHandler)
    ]


def test_rotates_daily_and_keeps_retention_days(tmp_path: Path) -> None:
    configure_logging(_settings(tmp_path, log_retention_days=7))

    handlers = _file_handlers()
    assert len(handlers) == 1
    assert handlers[0].when == "MIDNIGHT"
    assert handlers[0].backupCount == 7
    assert handlers[0].delay is True


def test_configure_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)
    configure_logging(settings)

    assert len(_file_handlers()) == 1


def test_writes_system_log_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)

    logging.getLogger("probe").info("hello-log-line")
    for handler in logging.getLogger().handlers:
        handler.flush()

    text = (settings.log_dir / "app.log").read_text(encoding="utf-8")
    assert "hello-log-line" in text


def test_shutdown_noise_is_filtered() -> None:
    record = logging.LogRecord("probe", logging.ERROR, __file__, 1, "boom", None, None)
    record.exc_info = (KeyboardInterrupt, KeyboardInterrupt(), None)

    assert ShutdownNoiseFilter().filter(record) is False


def test_normal_error_is_kept() -> None:
    record = logging.LogRecord("probe", logging.ERROR, __file__, 1, "real error", None, None)
    record.exc_info = (ValueError, ValueError("x"), None)

    assert ShutdownNoiseFilter().filter(record) is True
