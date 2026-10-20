"""工作流生命周期异常。

两类中断的语义：
- 暂停：已完成节点的产物保留在磁盘，恢复时从断点继续，不重跑；
- 取消：终止且不可恢复，重新分析需要重新上传。
"""


class TaskInterruptedError(Exception):
    """任务被中断（暂停或取消），工作流停止推进。"""


class TaskPausedError(TaskInterruptedError):
    """任务被暂停：产物保留，可由 resume 从断点继续。"""


class TaskCancelledError(TaskInterruptedError):
    """任务被取消：不可恢复，且不写入 error（用户主动行为）。"""
