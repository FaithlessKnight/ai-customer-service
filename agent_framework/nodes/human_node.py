"""
Human-in-the-Loop 节点

在图执行流程中插入人工审核/确认步骤。
利用 LangGraph interrupt 机制暂停图执行，等待人工输入。
"""

from typing import Any, Optional

from langgraph.types import interrupt

from agent_framework.state.base import BaseAgentState


class HumanInLoopNode:
    """
    人工介入节点。

    执行到此节点时图会暂停，等待外部通过 graph.invoke 或
    Command(resume=...) 提供人工输入后继续执行。

    Example:
        >>> human_node = HumanInLoopNode(prompt="请审核以上内容，输入 'approve' 确认或输入修改意见:")
        >>> builder.add_node("human_review", human_node)
    """

    def __init__(
        self,
        prompt: str = "请输入您的反馈（输入内容后继续执行）:",
        output_key: str = "user_input",
    ) -> None:
        """
        Args:
            prompt:     展示给人工的提示信息
            output_key: 人工输入结果写入 state 的字段名
        """
        self.prompt = prompt
        self.output_key = output_key

    def __call__(self, state: BaseAgentState) -> dict[str, Any]:
        """
        暂停图执行，等待人工输入。

        此函数调用 interrupt() 后图会挂起，
        直到外部调用 graph.invoke(Command(resume=value)) 才继续。

        Args:
            state: 当前图状态

        Returns:
            包含人工输入的状态更新字典
        """
        # interrupt 会暂停执行并将 prompt 传给外部调用者
        human_input = interrupt({
            "prompt": self.prompt,
            "current_output": state.get("agent_output", ""),
        })
        return {self.output_key: human_input}
