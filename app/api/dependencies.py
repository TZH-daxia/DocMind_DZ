"""FastAPI 依赖：进程级 TaskService 单例。"""

from functools import lru_cache

from app.config import get_settings
from app.service.task_service import TaskService


@lru_cache
def get_task_service() -> TaskService:
    return TaskService(get_settings())
