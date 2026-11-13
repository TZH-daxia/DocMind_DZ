"""系统日志：按天轮转、保留天数、关停噪音过滤。"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from app.config import Settings
from app.logging_config import (
    DailyFileHandler,
    ShutdownNoiseFilter,
    configure_logging,
    daily_log_path,
)


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(log_dir=tmp_path / "logs", log_console=False, **overrides)


def _file_handlers() -> list[DailyFileHandler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, DailyFileHandler)
    ]


def test_daily_file_name_contains_date(tmp_path: Path) -> None:
    """日志文件名带当天日期，不再写无日期的 app.log。"""

    configure_logging(_settings(tmp_path, log_retention_days=7))

    handlers = _file_handlers()
    assert len(handlers) == 1
    handler = handlers[0]
    assert handler.retention_days == 7
    assert handler.baseFilename.endswith(f"app.log.{date.today().isoformat()}")


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

    text = daily_log_path(settings.log_dir).read_text(encoding="utf-8")
    assert "hello-log-line" in text
    # 不生成无日期的 app.log
    assert not (settings.log_dir / "app.log").exists()


def test_prune_removes_expired_daily_logs(tmp_path: Path) -> None:
    """启动时清理超过保留天数的历史日志。"""

    settings = _settings(tmp_path, log_retention_days=2)
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    expired = daily_log_path(log_dir, (today - timedelta(days=5)).isoformat())
    keep = daily_log_path(log_dir, (today - timedelta(days=1)).isoformat())
    expired.write_text("expired", encoding="utf-8")
    keep.write_text("keep", encoding="utf-8")

    configure_logging(settings)

    assert not expired.exists()
    assert keep.exists()


def test_shutdown_noise_is_filtered() -> None:
    record = logging.LogRecord("probe", logging.ERROR, __file__, 1, "boom", None, None)
    record.exc_info = (KeyboardInterrupt, KeyboardInterrupt(), None)

    assert ShutdownNoiseFilter().filter(record) is False


def test_normal_error_is_kept() -> None:
    record = logging.LogRecord("probe", logging.ERROR, __file__, 1, "real error", None, None)
    record.exc_info = (ValueError, ValueError("x"), None)

    assert ShutdownNoiseFilter().filter(record) is True
