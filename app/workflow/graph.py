"""分析图：解析 → 图像转写 → 单据分类 → 字段抽取 → 一致性核对 → 结果装配。

固定线性顺序；断点续跑用「已完成节点」做条件入口，产物存在即视为该节点已完成。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from app.workflow.nodes import NodeHandler, build_node
from app.workflow.state import AnalysisState

NODE_SEQUENCE: tuple[tuple[str, str, int], ...] = (
    ("render_document", "解析文档", 5),
    ("transcribe_pages", "图像转写", 25),
    ("classify_pages", "识别主单/分单/舱单", 45),
    ("extract_documents", "抽取分单与舱单字段", 55),
    ("verify_consistency", "核对分单与舱单", 85),
    ("build_result", "生成结果", 95),
)

NODE_ORDER: tuple[str, ...] = tuple(item[0] for item in NODE_SEQUENCE)


def _first_pending_node(state: AnalysisState) -> str:
    completed = set(state.get("completed_nodes") or ())
    for node_name in NODE_ORDER:
        if node_name not in completed:
            return node_name
    return END


class AnalysisGraph:
    def __init__(self, handlers: dict[str, NodeHandler]) -> None:
        graph = StateGraph(AnalysisState)
        for index, (name, label, start) in enumerate(NODE_SEQUENCE):
            handler = handlers.get(name)
            if handler is None:
                raise ValueError(f"缺少节点处理器：{name}")
            # 节点完成时把进度推进到下一个节点的入口值（最后一个节点到 100）
            success = NODE_SEQUENCE[index + 1][2] if index + 1 < len(NODE_SEQUENCE) else 100
            graph.add_node(
                name,
                build_node(
                    name=name,
                    label=label,
                    start_progress=start,
                    success_progress=success,
                    handler=handler,
                ),
            )
        graph.set_conditional_entry_point(_first_pending_node)
        for index in range(len(NODE_SEQUENCE) - 1):
            graph.add_edge(NODE_SEQUENCE[index][0], NODE_SEQUENCE[index + 1][0])
        graph.add_edge(NODE_SEQUENCE[-1][0], END)
        self.graph = graph.compile()

    async def ainvoke(self, state: AnalysisState) -> dict[str, Any]:
        return await self.graph.ainvoke(state)
