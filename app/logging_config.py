"""日志初始化：按天写带日期的日志文件 + 可选控制台（仅真实终端）。

设计约定：
- 文件名始终带日期：`app.log.YYYY-MM-DD`，不写无日期的 `app.log`；
  这样目录里按文件名排序就是「旧 → 新」，最新一天在最下面；
- 跨天自动切到新文件，保留 N 天，启动时清理过期文件；
- `delay=True`：没有日志就不创建空文件；
- 控制台只在 stdout 是 TTY 时启用，避免 uvicorn --reload 的管道阻塞事件循环；
- 过滤正常关停时的 KeyboardInterrupt / CancelledError 噪音；
- 第三方库（uvicorn / httpx / openai）统一降到 WARNING。
"""

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import Settings

LOG_FILE_PREFIX = "app.log"


def daily_log_path(log_dir: str | Path, stamp: str | None = None) -> Path:
    """当天日志文件路径：`app.log.YYYY-MM-DD`（不带无日期的 app.log）。"""

    day = stamp or datetime.now().strftime("%Y-%m-%d")
    return Path(log_dir) / f"{LOG_FILE_PREFIX}.{day}"


class DailyFileHandler(logging.FileHandler):
    """按天切换文件名的日志 Handler（`app.log.YYYY-MM-DD`）。

    不用 `TimedRotatingFileHandler`：它固定写 `app.log`、滚动后才改名，
    目录里会同时有无日期的当前文件和带日期的历史文件；按文件名排序时
    `app.log` 永远排在最前，与「按日期递增、最新在最下」相反。
    """

    def __init__(
        self,
        log_dir: str | Path,
        retention_days: int = 14,
        encoding: str = "utf-8",
    ) -> None:
        self.log_dir = Path(log_dir)
        self.retention_days = max(int(retention_days), 1)
        self.stamp = datetime.now().strftime("%Y-%m-%d")
        super().__init__(
            daily_log_path(self.log_dir, self.stamp), encoding=encoding, delay=True
        )

    def emit(self, record: logging.LogRecord) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d")
        if stamp != self.stamp:
            self.stamp = stamp
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self.baseFilename = str(daily_log_path(self.log_dir, stamp))
            self.prune()
        super().emit(record)

    def prune(self) -> int:
        """删除超过保留天数的 `app.log.YYYY-MM-DD`，返回删除数量。"""

        cutoff = date.today() - timedelta(days=self.retention_days)
        removed = 0
        for path in self.log_dir.glob(f"{LOG_FILE_PREFIX}.*"):
            try:
                day = date.fromisoformat(path.name[len(LOG_FILE_PREFIX) + 1 :])
            except ValueError:
                # 不是日期后缀（如 .json / .bak），不动
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

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
    """配置系统日志：按天写 app.log.YYYY-MM-DD、保留 N 天；可选控制台。"""

    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    if any(getattr(handler, "_docmind_dz", False) for handler in root.handlers):
        return

    level = settings.log_level.upper()
    console = _console_enabled(settings)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    noise_filter = ShutdownNoiseFilter()

    file_handler = DailyFileHandler(log_dir, settings.log_retention_days)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(noise_filter)
    file_handler._docmind_dz = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(noise_filter)
        console_handler._docmind_dz = True  # type: ignore[attr-defined]
        root.addHandler(console_handler)

    root.setLevel(level)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    removed = file_handler.prune()
    logging.getLogger(__name__).info(
        "系统日志已初始化：level=%s retention=%s 天 console=%s file=%s 清理过期=%s",
        level,
        settings.log_retention_days,
        console,
        file_handler.baseFilename,
        removed,
    )
