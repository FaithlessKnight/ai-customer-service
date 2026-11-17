"""
预置图模板

提供开箱即用的常见智能体图结构：
- ReAct Agent:      思考-行动循环，自动调用工具
- RAG Agent:        检索增强生成（支持 Milvus / BM25 / 混合检索）
- Supervisor Agent: 多智能体协调者
"""

from typing import Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph

from agent_framework.graph.builder import GraphBuilder
from agent_framework.nodes.llm_node import LLMNode
from agent_framework.nodes.tool_node import AgentToolNode
from agent_framework.nodes.router_node import route_by_tool_call
from agent_framework.state.base import BaseAgentState


def build_react_agent(
    llm: BaseChatModel,
    tools: list[BaseTool],
    system_prompt: Optional[str] = None,
    state_schema=None,
    checkpointer=None,
    max_iterations: int = 10,
) -> CompiledStateGraph:
    """
    构建标准 ReAct（Reasoning + Acting）智能体图。

    图结构:
        START -> llm -> [tool_call?] -> tools -> llm -> ... -> END
    """
    schema = state_schema or BaseAgentState
    llm_node = LLMNode(llm=llm, system_prompt=system_prompt, tools=tools)
    tool_node = AgentToolNode(tools=tools)

    graph = (
        GraphBuilder(schema)
        .add_node("llm", llm_node)
        .add_node("tools", tool_node)
        .set_entry_point("llm")
        .add_conditional_edges(
            "llm",
            route_by_tool_call,
            {"tools": "tools", "end": END},
        )
        .add_edge("tools", "llm")
        .compile(checkpointer=checkpointer)
    )
    return graph


def build_rag_agent(
    llm: BaseChatModel,
    retriever=None,
    system_prompt: Optional[str] = None,
    tools: Optional[list[BaseTool]] = None,
    retrieve_k: int = 5,
    state_schema=None,
    checkpointer=None,
) -> CompiledStateGraph:
    """
    构建 RAG（检索增强生成）智能体图。

    图结构:
        START -> retrieve -> llm -> END

    Args:
        llm:           ChatModel 实例
        retriever:     BaseRetriever 实例。支持：
                         - MilvusRetriever（纯向量）
                         - BM25Retriever（纯关键词）
                         - HybridRetriever（混合，生产推荐）
                       为 None 时默认创建 HybridRetriever。
        system_prompt: 系统提示词
        tools:         可选额外工具（注入 LLM 节点）
        retrieve_k:    每次检索返回文档数量
        state_schema:  自定义状态类
        checkpointer:  检查点存储

    Returns:
        编译好的 RAG 图
    """
    if retriever is None:
        from agent_framework.memory.hybrid_retriever import HybridRetriever
        retriever = HybridRetriever()

    schema = state_schema or BaseAgentState
    rag_system = system_prompt or "你是一个知识库问答助手，请根据以下检索到的相关内容回答用户问题。"

    def retrieve_node(state: BaseAgentState) -> dict:
        """检索节点：调用 retriever 检索相关上下文（支持混合检索）。"""
        user_input = state.get("user_input", "")
        if not user_input:
            messages = state.get("messages", [])
            if messages:
                last = messages[-1]
                user_input = last.content if hasattr(last, "content") else str(last)
        context = retriever.format_context(user_input, k=retrieve_k)
        return {"context": [context]}

    def rag_llm_node(state: BaseAgentState) -> dict:
        """RAG LLM 节点：将检索内容注入 prompt 后调用 LLM。"""
        context = "\n".join(state.get("context", []))
        full_system = f"{rag_system}\n\n相关知识:\n{context}"
        node = LLMNode(llm=llm, system_prompt=full_system, tools=tools)
        return node(state)

    graph = (
        GraphBuilder(schema)
        .add_node("retrieve", retrieve_node)
        .add_node("llm", rag_llm_node)
        .set_entry_point("retrieve")
        .add_edge("retrieve", "llm")
        .set_finish_point("llm")
        .compile(checkpointer=checkpointer)
    )
    return graph


def build_supervisor_agent(
    supervisor_llm: BaseChatModel,
    worker_agents: dict[str, CompiledStateGraph],
    system_prompt: Optional[str] = None,
    state_schema=None,
    checkpointer=None,
) -> CompiledStateGraph:
    """
    构建 Supervisor 多智能体协调图。

    图结构:
        START -> supervisor -> [worker_a | worker_b | ...] -> supervisor -> END
    """
    schema = state_schema or BaseAgentState
    worker_names = list(worker_agents.keys())
    default_system = (
        f"你是一个多智能体协调者。可用的专家智能体有: {', '.join(worker_names)}。\n"
        "根据用户请求，在 next_node 字段中指定要调用的智能体名称，"
        "或者填写 'FINISH' 表示任务完成。"
    )

    def supervisor_node(state: BaseAgentState) -> dict:
        prompt = system_prompt or default_system
        node = LLMNode(llm=supervisor_llm, system_prompt=prompt)
        result = node(state)
        output = result.get("agent_output", "")
        next_node = "FINISH"
        for name in worker_names:
            if name.lower() in output.lower():
                next_node = name
                break
        return {**result, "next_node": next_node}

    def supervisor_router(state: BaseAgentState) -> str:
        next_node = state.get("next_node", "FINISH")
        if next_node == "FINISH" or next_node not in worker_names:
            return "end"
        return next_node

    builder = GraphBuilder(schema)
    builder.add_node("supervisor", supervisor_node)

    for name, agent in worker_agents.items():
        def make_worker_node(ag):
            def worker_node(state):
                result = ag.invoke(state)
                return {
                    "messages": result.get("messages", []),
                    "agent_output": result.get("agent_output", ""),
                    "next_node": "supervisor",
                }
            return worker_node
        builder.add_node(name, make_worker_node(agent))
        builder.add_edge(name, "supervisor")

    route_map = {name: name for name in worker_names}
    route_map["end"] = END
    builder.set_entry_point("supervisor")
    builder.add_conditional_edges("supervisor", supervisor_router, route_map)

    return builder.compile(checkpointer=checkpointer)
