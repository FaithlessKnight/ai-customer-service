"""
对话历史记忆模块

管理多轮对话的消息历史，支持窗口截断和摘要压缩。
"""

from typing import Optional

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    AIMessage,
    SystemMessage,
    trim_messages,
)


class ConversationMemory:
    """
    对话记忆管理器。

    功能:
    - 维护完整对话历史
    - 按窗口大小截断（防止超出上下文长度）
    - 可选系统提示词前缀

    Example:
        >>> mem = ConversationMemory(max_messages=20)
        >>> mem.add_user_message("你好")
        >>> mem.add_ai_message("你好！有什么可以帮您？")
        >>> history = mem.get_messages()
    """

    def __init__(
        self,
        max_messages: int = 50,
        system_prompt: Optional[str] = None,
    ) -> None:
        """
        Args:
            max_messages:  最大保留消息条数（超出后从头部裁剪）
            system_prompt: 系统提示词（始终置于消息列表首部）
        """
        self.max_messages = max_messages
        self._messages: list[BaseMessage] = []
        self._system_prompt = system_prompt

    def add_user_message(self, content: str) -> None:
        """添加用户消息。"""
        self._messages.append(HumanMessage(content=content))
        self._truncate()

    def add_ai_message(self, content: str) -> None:
        """添加 AI 消息。"""
        self._messages.append(AIMessage(content=content))
        self._truncate()

    def add_message(self, message: BaseMessage) -> None:
        """添加任意类型消息。"""
        self._messages.append(message)
        self._truncate()

    def add_messages(self, messages: list[BaseMessage]) -> None:
        """批量添加消息。"""
        self._messages.extend(messages)
        self._truncate()

    def get_messages(self, include_system: bool = True) -> list[BaseMessage]:
        """获取完整对话历史（含系统提示词）。"""
        if include_system and self._system_prompt:
            return [SystemMessage(content=self._system_prompt)] + self._messages
        return list(self._messages)

    def get_last_n(self, n: int) -> list[BaseMessage]:
        """获取最近 n 条消息。"""
        return self._messages[-n:]

    def clear(self) -> None:
        """清空对话历史（保留系统提示词）。"""
        self._messages.clear()

    def set_system_prompt(self, prompt: str) -> None:
        """更新系统提示词。"""
        self._system_prompt = prompt

    def _truncate(self) -> None:
        """超出 max_messages 时从头部裁剪，保留最新消息。"""
        if len(self._messages) > self.max_messages:
            self._messages = self._messages[-self.max_messages:]

    def __len__(self) -> int:
        return len(self._messages)

    def __repr__(self) -> str:
        return f"ConversationMemory(messages={len(self._messages)}, max={self.max_messages})"
