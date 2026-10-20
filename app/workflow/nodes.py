"""节点包装：统一处理中断检查、进度上报、耗时统计与「已完成节点」记账。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from app.workflow.errors import TaskInterruptedError
from app.workflow.state import AnalysisState

NodeHandler = Callable[[AnalysisState], Awaitable[dict[str, Any]]]


def build_node(
    *,
    name: str,
    label: str,
    start_progress: int,
    success_progress: int,
    handler: NodeHandler,
) -> Callable[[AnalysisState], Awaitable[dict[str, Any]]]:
    async def run(state: AnalysisState) -> dict[str, Any]:
        completed = list(state.get("completed_nodes") or [])
        if name in completed:
            return {}
        reporter = state.get("reporter")
        if reporter is not None:
            # 节点入口的中断检查：暂停/取消在下一个节点边界生效
            await reporter.checkpoint()
            await reporter.emit(label, f"{label}…", start_progress, phase="started")
        started_clock = perf_counter()
        try:
            update = await handler(state) or {}
        except TaskInterruptedError:
            # 暂停/取消是用户行为，不记失败事件
            raise
        except Exception as exc:  # noqa: BLE001 - 统一记录失败耗时后原样抛出
            duration_ms = int((perf_counter() - started_clock) * 1000)
            if reporter is not None:
                await reporter.emit(
                    label,
                    f"{label} 执行失败：{exc}",
                    start_progress,
                    level="error",
                    phase="failed",
                    duration_ms=duration_ms,
                )
            raise
        duration_ms = int((perf_counter() - started_clock) * 1000)
        update = dict(update)
        update["completed_nodes"] = [*completed, name]
        if reporter is not None:
            await reporter.emit(
                label,
                f"{label} 完成",
                success_progress,
                phase="succeeded",
                duration_ms=duration_ms,
            )
        return update

    run.__name__ = f"node_{name}"
    return run
