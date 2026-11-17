"""
基础智能体抽象类

所有具体智能体的父类，定义统一的接口契约。
"""

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Iterator, Optional

from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph


class BaseAgent(ABC):
    """
    基础智能体抽象类。

    子类需实现 build_graph() 方法，其余功能（invoke/stream/chat）由基类提供。

    Example:
        >>> class MyAgent(BaseAgent):
        ...     def build_graph(self):
        ...         # 构建并返回 CompiledStateGraph
        ...         ...
        >>> agent = MyAgent()
        >>> response = agent.chat("你好")
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        verbose: bool = False,
    ) -> None:
        """
        Args:
            session_id: 会话 ID，用于多轮对话记忆隔离
            verbose:    是否打印调试信息
        """
        self.session_id = session_id or "default"
        self.verbose = verbose
        self._graph: Optional[CompiledStateGraph] = None

    @property
    def graph(self) -> CompiledStateGraph:
        """懒加载图（首次访问时构建）。"""
        if self._graph is None:
            self._graph = self.build_graph()
        return self._graph

    @abstractmethod
    def build_graph(self) -> CompiledStateGraph:
        """
        构建并返回编译好的 LangGraph 图。
        子类必须实现此方法。
        """
        ...

    def _make_config(self, session_id: Optional[str] = None) -> dict:
        """生成 LangGraph 调用配置（携带 thread_id 实现多轮记忆）。"""
        sid = session_id or self.session_id
        return {"configurable": {"thread_id": sid}}

    def invoke(
        self,
        input_state: dict[str, Any],
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        同步调用图，返回最终状态。

        Args:
            input_state: 初始状态字典
            session_id:  覆盖默认会话 ID

        Returns:
            最终状态字典
        """
        config = self._make_config(session_id)
        return self.graph.invoke(input_state, config=config)

    def stream(
        self,
        input_state: dict[str, Any],
        session_id: Optional[str] = None,
    ) -> Iterator[dict[str, Any]]:
        """
        流式调用图，逐步产出每个节点的状态更新。

        Args:
            input_state: 初始状态字典
            session_id:  覆盖默认会话 ID

        Yields:
            每个节点执行后的状态更新字典
        """
        config = self._make_config(session_id)
        yield from self.graph.stream(input_state, config=config)

    def chat(
        self,
        user_message: str,
        session_id: Optional[str] = None,
    ) -> str:
        """
        便捷方法：传入用户文本，返回 AI 回复文本。

        Args:
            user_message: 用户输入的自然语言消息
            session_id:   覆盖默认会话 ID

        Returns:
            AI 回复内容字符串
        """
        state = {"messages": [HumanMessage(content=user_message)]}
        result = self.invoke(state, session_id=session_id)
        messages = result.get("messages", [])
        if messages:
            last = messages[-1]
            return last.content if hasattr(last, "content") else str(last)
        return result.get("agent_output", "")

    def reset(self) -> None:
        """重置图（下次访问时重新构建）。"""
        self._graph = None
