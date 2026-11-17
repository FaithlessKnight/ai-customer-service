"""
图构建器

对 LangGraph StateGraph 的封装，提供流畅的链式 API，
简化图的节点、边、条件路由的定义过程。
"""

from typing import Any, Callable, Optional, Type

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent_framework.state.base import BaseAgentState


class GraphBuilder:
    """
    LangGraph 图构建器，提供链式 API。

    Example:
        >>> from agent_framework.graph import GraphBuilder
        >>> from agent_framework.state import BaseAgentState
        >>>
        >>> graph = (
        ...     GraphBuilder(BaseAgentState)
        ...     .add_node("llm", llm_node)
        ...     .add_node("tools", tool_node)
        ...     .set_entry_point("llm")
        ...     .add_conditional_edges(
        ...         "llm", router_fn,
        ...         {"tools": "tools", "end": END}
        ...     )
        ...     .add_edge("tools", "llm")
        ...     .compile()
        ... )
    """

    def __init__(self, state_schema: Type[BaseAgentState] = BaseAgentState) -> None:
        """
        Args:
            state_schema: TypedDict 状态类，定义图中共享的数据结构
        """
        self._graph = StateGraph(state_schema)
        self._compiled: Optional[CompiledStateGraph] = None

    def add_node(self, name: str, node: Callable) -> "GraphBuilder":
        """
        添加节点。

        Args:
            name: 节点名称（在边定义中引用）
            node: 可调用对象，接受 state 返回状态更新字典
        """
        self._graph.add_node(name, node)
        return self

    def add_edge(self, from_node: str, to_node: str) -> "GraphBuilder":
        """
        添加固定边（无条件跳转）。

        Args:
            from_node: 源节点名称
            to_node:   目标节点名称（可用 END 常量）
        """
        self._graph.add_edge(from_node, to_node)
        return self

    def add_conditional_edges(
        self,
        from_node: str,
        condition: Callable,
        mapping: dict[str, str],
    ) -> "GraphBuilder":
        """
        添加条件边（路由）。

        Args:
            from_node:  源节点名称
            condition:  路由函数，接受 state 返回映射键字符串
            mapping:    路由键 -> 目标节点名称的映射字典
        """
        self._graph.add_conditional_edges(from_node, condition, mapping)
        return self

    def set_entry_point(self, node: str) -> "GraphBuilder":
        """
        设置图入口节点（等价于 add_edge(START, node)）。

        Args:
            node: 入口节点名称
        """
        self._graph.add_edge(START, node)
        return self

    def set_finish_point(self, node: str) -> "GraphBuilder":
        """
        设置图出口节点（等价于 add_edge(node, END)）。

        Args:
            node: 终止节点名称
        """
        self._graph.add_edge(node, END)
        return self

    def compile(
        self,
        checkpointer=None,
        interrupt_before: Optional[list[str]] = None,
        interrupt_after: Optional[list[str]] = None,
    ) -> CompiledStateGraph:
        """
        编译图，生成可执行的 CompiledStateGraph。

        Args:
            checkpointer:      检查点存储（支持多轮对话记忆，见 memory.checkpointer）
            interrupt_before:  在这些节点执行前暂停（Human-in-Loop）
            interrupt_after:   在这些节点执行后暂停

        Returns:
            编译好的图实例，可直接调用 .invoke() / .stream()
        """
        self._compiled = self._graph.compile(
            checkpointer=checkpointer,
            interrupt_before=interrupt_before or [],
            interrupt_after=interrupt_after or [],
        )
        return self._compiled

    def get_graph(self) -> StateGraph:
        """获取原始 StateGraph（编译前）。"""
        return self._graph

    def visualize(self) -> None:
        """
        在终端打印图的 ASCII 结构（需要先 compile）。
        如果安装了 graphviz，可生成可视化图片。
        """
        if not self._compiled:
            print("[提示] 请先调用 .compile() 再可视化")
            return
        try:
            graph_png = self._compiled.get_graph().draw_mermaid_png()
            with open("graph.png", "wb") as f:
                f.write(graph_png)
            print("[成功] 图已保存为 graph.png")
        except Exception:
            print(self._compiled.get_graph().draw_mermaid())
