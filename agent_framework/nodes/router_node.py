"""
路由节点

提供条件路由逻辑，决定图的下一步走向。
包含预置路由策略和自定义路由支持。
"""

from typing import Any, Callable, Literal

from langchain_core.messages import AIMessage

from agent_framework.state.base import BaseAgentState


class RouterNode:
    """
    条件路由节点。

    根据当前状态决定下一个节点，
    可传入自定义路由函数或使用内置策略。

    Example:
        >>> router = RouterNode(route_fn=my_route_fn)
        >>> # 或使用内置 tool_call 路由
        >>> router = RouterNode(strategy="tool_call")
        >>> builder.add_conditional_edges("llm", router, {"tools": "tools", "end": END})
    """

    def __init__(
        self,
        route_fn: Callable[[BaseAgentState], str] = None,
        strategy: Literal["tool_call", "should_end", "next_node"] = "tool_call",
    ) -> None:
        """
        Args:
            route_fn:  自定义路由函数，接受 state 返回路由 key 字符串
            strategy:  内置路由策略（route_fn 为 None 时生效）
                       - 'tool_call':  检测 LLM 是否发起工具调用
                       - 'should_end': 检测 state.should_end 字段
                       - 'next_node':  读取 state.next_node 字段
        """
        self._route_fn = route_fn
        self._strategy = strategy

    def __call__(self, state: BaseAgentState) -> str:
        """
        执行路由判断，返回下一个节点的 key。

        Returns:
            路由目标字符串，需与 add_conditional_edges 的映射字典键匹配
        """
        if self._route_fn:
            return self._route_fn(state)

        if self._strategy == "tool_call":
            return self._route_by_tool_call(state)
        elif self._strategy == "should_end":
            return "end" if state.get("should_end") else "continue"
        elif self._strategy == "next_node":
            return state.get("next_node", "end")
        return "end"

    @staticmethod
    def _route_by_tool_call(state: BaseAgentState) -> str:
        """
        检测最后一条 AI 消息是否包含工具调用。

        Returns:
            'tools'  - 需要执行工具
            'end'    - 直接结束
        """
        messages = state.get("messages", [])
        if not messages:
            return "end"
        last_message = messages[-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "tools"
        return "end"


def route_by_tool_call(state: BaseAgentState) -> str:
    """独立路由函数（可直接传给 add_conditional_edges）。"""
    return RouterNode._route_by_tool_call(state)
