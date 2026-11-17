"""
工具执行节点

封装 LangGraph 内置 ToolNode，提供错误处理和结果格式化。
在智能体图中负责实际执行 LLM 请求的工具调用。
"""

from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.prebuilt import ToolNode

from agent_framework.state.base import BaseAgentState


class AgentToolNode:
    """
    工具执行节点，封装 LangGraph 内置 ToolNode。

    自动处理 LLM 返回的 tool_calls，执行对应工具，
    并将结果以 ToolMessage 形式写回消息列表。

    Example:
        >>> from agent_framework.tools import calculator_tool, web_search_tool
        >>> tool_node = AgentToolNode(tools=[calculator_tool, web_search_tool])
        >>> builder.add_node("tools", tool_node)
    """

    def __init__(self, tools: list, handle_tool_errors: bool = True) -> None:
        """
        Args:
            tools:             工具列表（与 LLMNode 绑定的工具保持一致）
            handle_tool_errors: 工具报错时是否优雅处理（返回错误消息而非抛出异常）
        """
        self._node = ToolNode(tools=tools, handle_tool_errors=handle_tool_errors)

    def __call__(self, state: BaseAgentState) -> dict[str, Any]:
        """
        执行工具调用并返回状态更新。

        Args:
            state: 当前图状态（messages 中最后一条应含 tool_calls）

        Returns:
            包含 ToolMessage 列表的状态更新字典
        """
        result = self._node.invoke(state)
        # 提取工具结果，写入 tool_results 字段供后续节点使用
        tool_results = []
        for msg in result.get("messages", []):
            if isinstance(msg, ToolMessage):
                tool_results.append({
                    "tool_call_id": msg.tool_call_id,
                    "name": msg.name,
                    "content": msg.content,
                })
        return {**result, "tool_results": tool_results}
