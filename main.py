"""启动入口：uvicorn app.main:app。"""

import os
import time

import uvicorn

from app.config import get_settings

# 进程启动时间点：app.main 用它算出「导入 + 装配」总耗时，写进启动日志
os.environ.setdefault("DOCMIND_DZ_PROCESS_STARTED_AT", f"{time.time():.3f}")


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
        log_level="info",
        # 有 SSE 长连接时不让 Ctrl+C 无限等待，最多优雅关闭 5 秒
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
