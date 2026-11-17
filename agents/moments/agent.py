"""
朋友圈互动智能体

图结构：
    emotion_node → route_after_emotion
                     ↙              ↘
             (positive)          (negative)
             rag_node              END
                 ↓
           generate_node
                 ↓
               END

无 checkpointer：朋友圈交互为无状态单次请求，不需要多轮记忆。
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from agents.moments.state import MomentsState
from agents.moments.nodes.emotion_node import moments_emotion_node
from agents.moments.nodes.rag_node import moments_rag_node
from agents.moments.nodes.generate_node import moments_generate_node

logger = logging.getLogger(__name__)


def _route_after_emotion(state: MomentsState) -> str:
    """积极 → 进入检索；消极 → 直接结束。"""
    return "rag" if state.get("is_positive") else END


def _build_graph():
    graph = StateGraph(MomentsState)
    graph.add_node("emotion",  moments_emotion_node)
    graph.add_node("rag",      moments_rag_node)
    graph.add_node("generate", moments_generate_node)

    graph.set_entry_point("emotion")
    graph.add_conditional_edges(
        "emotion",
        _route_after_emotion,
        {"rag": "rag", END: END},
    )
    graph.add_edge("rag", "generate")
    graph.add_edge("generate", END)

    return graph.compile()   # 无 checkpointer，无状态单次请求


class MomentsAgent:
    """朋友圈点赞/评论智能体。"""

    def __init__(self) -> None:
        self._graph = _build_graph()
        logger.info("MomentsAgent 图编译完成")

    async def aprocess(self, moment_content: str, session_id: str = "default") -> dict[str, Any]:
        """
        处理单条朋友圈，返回点赞决策和评论。

        Returns:
            {"like": bool, "comment": str | None, "session_id": str}
        """
        state_input: MomentsState = {
            "moment_content":  moment_content,
            "session_id":      session_id,
            "is_positive":     False,
            "retrieved_chunks": [],
            "comment":         None,
            "debug_info":      {},
        }
        result     = await self._graph.ainvoke(state_input)
        is_positive = result.get("is_positive", False)
        return {
            "like":       is_positive,
            "comment":    result.get("comment") if is_positive else None,
            "session_id": session_id,
        }
