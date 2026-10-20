"""工作流进度事件协议。"""

from typing import Literal, Protocol

EventLevel = Literal["info", "warning", "error"]


class ProgressReporter(Protocol):
    async def emit(
        self,
        step: str,
        message: str,
        progress: int,
        level: EventLevel = "info",
    ) -> None: ...
