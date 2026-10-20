"""FastAPI 装配。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.dependencies import get_task_service
from app.api.routes.analysis import router as analysis_router
from app.config import get_settings
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    service = get_task_service()
    if service.pipeline is None:
        logger.warning("服务已启动，但模型尚未配置：%s", service._gateway_error)
    else:
        logger.info("服务启动完成，模型网关就绪（mock=%s）", settings.mock_llm)
    asyncio.create_task(asyncio.to_thread(service.cleanup_expired))
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)
    app = FastAPI(
        title=settings.app_name,
        description="主单/分单/舱单识别：跳过主单，抽取多份分单与一份舱单并核对字段",
        version="0.1.0",
        lifespan=lifespan,
    )
    # 前端独立域名部署时浏览器直连本服务：不放开跨域会被拦在预检。
    # 不开 allow_credentials：接口不依赖 Cookie，保持最小放行面。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=settings.cors_origin_regex,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(analysis_router, prefix=settings.api_prefix)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, object]:
        service = get_task_service()
        return {
            "status": "ok",
            "app": settings.app_name,
            "mock_llm": settings.mock_llm,
            "model_configured": service.pipeline is not None,
            "api_prefix": settings.api_prefix,
        }

    @app.middleware("http")
    async def no_cache_static_assets(request: Request, call_next):
        """静态资源不做浏览器强缓存，避免改版后仍加载旧 JS/CSS。"""

        response = await call_next(request)
        path = request.url.path
        if request.method == "GET" and not path.startswith(settings.api_prefix):
            if path == "/" or path.endswith((".html", ".js", ".css")):
                response.headers["Cache-Control"] = "no-store"
        return response

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
