"""
状态管理模块

定义 LangGraph 图中各节点共享的状态结构。
使用 TypedDict + Annotated 配合 reducer 函数实现状态合并。
"""

from enum import Enum
from typing import Annotated, Any, Optional, Sequence

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages
from typing_extensions import TypedDict


class MessageRole(str, Enum):
    """消息角色枚举。"""
    HUMAN = "human"
    AI = "ai"
    SYSTEM = "system"
    TOOL = "tool"


class AgentMessage(TypedDict, total=False):
    """简化的消息数据类（用于序列化/展示）。"""
    role: str
    content: str
    name: Optional[str]
    tool_call_id: Optional[str]
    metadata: dict[str, Any]


class BaseAgentState(TypedDict, total=False):
    """
    基础智能体状态。

    所有自定义 State 应继承此类并按需扩展。

    字段说明:
        messages:       对话历史（自动追加，不覆盖）
        user_input:     当前用户输入
        agent_output:   智能体最终输出
        context:        检索到的上下文（RAG 场景）
        next_node:      条件路由目标节点名
        should_end:     是否终止图执行
        iteration:      当前迭代次数（防死循环）
        tool_results:   工具调用结果列表
        metadata:       任意附加元数据
        error:          执行过程中的错误信息
    """

    # 核心消息历史 —— add_messages reducer 自动追加而非覆盖
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # 当前输入/输出
    user_input: str
    agent_output: str

    # RAG / 知识库上下文
    context: list[str]

    # 路由与控制流
    next_node: str
    should_end: bool
    iteration: int

    # 工具调用结果
    tool_results: list[dict[str, Any]]

    # 通用元数据
    metadata: dict[str, Any]
    error: Optional[str]


class CustomerServiceState(BaseAgentState, total=False):
    """
    客服场景专用状态（示例扩展）。

    在 BaseAgentState 基础上新增客服业务字段。
    """
    session_id: str           # 会话 ID
    user_id: str              # 用户 ID
    intent: str               # 识别出的用户意图
    sentiment: str            # 情感分析结果: positive / neutral / negative
    escalate: bool            # 是否需要转人工
    ticket_id: Optional[str]  # 工单 ID
    language: str             # 用户语言偏好
