"""
LLM 节点

封装 LLM 调用逻辑，支持工具绑定、系统提示词注入。
作为 LangGraph 节点函数使用。
"""

from typing import Any, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool

from agent_framework.state.base import BaseAgentState


class LLMNode:
    """
    LLM 节点：封装模型调用，可绑定工具。

    Example:
        >>> from agent_framework.config.llm_factory import create_llm
        >>> llm = create_llm()
        >>> node = LLMNode(llm=llm, system_prompt="你是一个客服助手")
        >>> # 在 GraphBuilder 中使用
        >>> builder.add_node("llm", node)
    """

    def __init__(
        self,
        llm: BaseChatModel,
        system_prompt: Optional[str] = None,
        tools: Optional[list[BaseTool]] = None,
        output_key: str = "agent_output",
    ) -> None:
        """
        Args:
            llm:           LangChain ChatModel 实例
            system_prompt: 系统提示词（每次调用自动插入消息列表头部）
            tools:         绑定的工具列表（启用 tool calling）
            output_key:    写入 state 的输出字段名
        """
        self.system_prompt = system_prompt
        self.output_key = output_key
        # 如果有工具则绑定，否则直接使用 llm
        self._llm = llm.bind_tools(tools) if tools else llm

    def __call__(self, state: BaseAgentState) -> dict[str, Any]:
        """
        节点执行函数，LangGraph 自动调用。

        Args:
            state: 当前图状态

        Returns:
            状态更新字典
        """
        messages = list(state.get("messages", []))

        # 注入系统提示词
        if self.system_prompt:
            messages = [SystemMessage(content=self.system_prompt)] + [
                m for m in messages if not isinstance(m, SystemMessage)
            ]

        response = self._llm.invoke(messages)

        return {
            "messages": [response],
            self.output_key: response.content,
        }
