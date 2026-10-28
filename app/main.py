"""FastAPI 装配。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.dependencies import get_task_service
from app.api.routes.analysis import router as analysis_router
from app.config import Settings, _local_ipv4_addresses, get_settings
from app.logging_config import LOG_FILE_NAME, configure_logging
from app.service.task_service import TaskService

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
# 模块加载起点：用于在启动日志里给出「导入 + 装配」总耗时，方便排查重启慢
_STARTED_AT = time.perf_counter()
# 进程启动时间由根目录 main.py 写入；直接 uvicorn app.main:app 启动时为空
_PROCESS_STARTED_AT = float(os.environ.get("DOCMIND_DZ_PROCESS_STARTED_AT") or 0.0)

# 周期清理间隔（小时）：与托书识别（DocMind）一致 —— 启动时先清一次，之后每小时一次
DATA_CLEANUP_INTERVAL_HOURS = 1.0


def _startup_elapsed() -> float:
    """从进程启动（拿不到则从模块导入）到当前时刻的耗时。"""

    if _PROCESS_STARTED_AT:
        return max(time.time() - _PROCESS_STARTED_AT, 0.0)
    return time.perf_counter() - _STARTED_AT


def _display_hosts(host: str) -> list[str]:
    """终端展示用的主机列表。

    配置 `0.0.0.0` / `::` 表示监听所有网卡，这个地址本身浏览器访问不了，
    所以先给回环地址，再补上本机的局域网 IP（同一网段的其他机器可用）。
    """

    if host not in {"", "0.0.0.0", "::", "[::]"}:
        return [host]
    hosts = ["127.0.0.1"]
    for address in _local_ipv4_addresses():
        if address not in hosts:
            hosts.append(address)
    return hosts


async def _periodic_data_cleanup(service: TaskService, interval_hours: float) -> None:
    """周期清理超过保留期的任务产物；单次失败只记日志，不影响服务。"""

    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            await asyncio.to_thread(service.run_data_retention_cleanup)
        except Exception:
            logger.exception("周期清理任务产物失败")


def format_startup_banner(current: Settings) -> str:
    """拼装启动提示：访问地址、接口文档、健康检查、日志与数据目录。"""

    port = current.app_port
    hosts = _display_hosts(current.app_host)
    primary = hosts[0]
    log_path = (Path(current.log_dir) / LOG_FILE_NAME).resolve()
    data_path = Path(current.data_root).resolve()
    # 实时看日志的命令随平台给出：部署在 Linux 容器里时 PowerShell 写法不可用
    watch_log = (
        f"Get-Content {log_path} -Wait -Tail 20"
        if os.name == "nt"
        else f"tail -f {log_path}"
    )
    lines = [
        f"{current.app_name} 已启动",
        f"  前端页面：http://{primary}:{port}/",
    ]
    if len(hosts) > 1:
        lan = "、".join(f"http://{host}:{port}/" for host in hosts[1:])
        lines.append(f"  局域网访问：{lan}")
    lines.extend(
        [
            f"  接口文档：http://{primary}:{port}/docs",
            f"  健康检查：http://{primary}:{port}/health",
            f"  系统日志：{log_path}",
            f"  任务数据：{data_path / 'tasks'}",
            f"  （实时查看日志：{watch_log}）",
        ]
    )
    return "\n".join(lines)


def _echo(message: str) -> None:
    """向终端输出提示；宿主终端不可用时静默失败，不影响服务。"""

    try:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        logger.debug("终端提示输出失败：%s", message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    service = get_task_service()
    if service.pipeline is None:
        logger.warning("服务已启动，但模型尚未配置：%s", service._gateway_error)
    else:
        logger.info(
            "服务启动完成，模型网关就绪（启动耗时 %.2fs）",
            _startup_elapsed(),
        )
    _echo(format_startup_banner(settings))
    # 启动先清一次过期产物；之后由周期任务每小时清一次（与托书识别一致）
    try:
        removed = await asyncio.to_thread(service.run_data_retention_cleanup)
        if removed:
            logger.info("启动清理：删除 %s 个过期条目", removed)
    except Exception:
        logger.exception("启动清理任务产物失败")
    cleanup_task = asyncio.create_task(
        _periodic_data_cleanup(service, DATA_CLEANUP_INTERVAL_HOURS)
    )
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        _echo(f"{settings.app_name} 已停止")


def _use_binary_format(node: Any) -> None:
    """把 OAS 3.1 的 `contentMediaType` 改写成 OAS 3.0 的 `format: binary`。

    Swagger UI 不认 `contentMediaType`：上传字段会被当成普通字符串，数组项还会
    预填一串随机二进制（页面上看就是乱码）。改成 `format: binary` 才会渲染成
    文件选择框。
    """

    if isinstance(node, dict):
        if node.get("contentMediaType") == "application/octet-stream":
            node.pop("contentMediaType")
            node["format"] = "binary"
        for value in list(node.values()):
            _use_binary_format(value)
    elif isinstance(node, list):
        for value in node:
            _use_binary_format(value)


def _patch_openapi_binary(app: FastAPI) -> None:
    """在生成 OpenAPI 后修正上传字段，文档里不再出现二进制乱码。"""

    original = app.openapi

    def openapi() -> dict[str, Any]:
        schema = original()
        _use_binary_format(schema)
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)
    app = FastAPI(
        title=settings.app_name,
        description="主单/分单/舱单识别：跳过主单，抽取多份分单与一份舱单并核对字段",
        version="0.1.0",
        lifespan=lifespan,
    )
    _patch_openapi_binary(app)
    # 前端独立域名部署时浏览器直连本服务：不放开跨域会被拦在预检。
    # 开发调试可用 DOCMIND_DZ_CORS_ALLOW_ALL=true 放开全部来源。
    # 不开 allow_credentials：接口不依赖 Cookie，保持最小放行面。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=settings.cors_origin_regex,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(analysis_router, prefix=settings.api_prefix)

    @app.get("/health", tags=["运维"], summary="健康检查")
    async def health() -> dict[str, object]:
        """返回服务状态、模型是否就绪与接口前缀。"""

        service = get_task_service()
        return {
            "status": "ok",
            "app": settings.app_name,
            "model_configured": service.pipeline is not None,
            "api_prefix": settings.api_prefix,
        }

    @app.middleware("http")
    async def json_charset_utf8(request: Request, call_next):
        """JSON 响应显式声明 UTF-8：部分客户端缺省按本地编码解码，中文会变乱码。"""

        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("application/json") and "charset" not in content_type:
            response.headers["content-type"] = f"{content_type}; charset=utf-8"
        return response

    @app.middleware("http")
    async def no_cache_static_assets(request: Request, call_next):
        """静态资源不做浏览器强缓存，避免改版后仍加载旧 JS/CSS。"""

        response = await call_next(request)
        path = request.url.path
        if (
            request.method == "GET"
            and not path.startswith(settings.api_prefix)
            and (path == "/" or path.endswith((".html", ".js", ".css")))
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
