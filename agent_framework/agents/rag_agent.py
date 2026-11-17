"""
RAG 智能体

检索增强生成智能体，先从知识库检索相关内容再生成回答。
支持 MilvusRetriever（向量）、BM25Retriever（关键词）、
HybridRetriever（混合，推荐生产使用）三种检索后端。
"""

from typing import Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from agent_framework.agents.base import BaseAgent
from agent_framework.config.llm_factory import create_llm
from agent_framework.graph.presets import build_rag_agent
from agent_framework.memory.base_retriever import BaseRetriever
from agent_framework.memory.checkpointer import get_checkpointer


class RagAgent(BaseAgent):
    """
    RAG 智能体：先检索知识库，再基于上下文回答。

    检索器通过 retriever 参数注入，默认使用 HybridRetriever
    （Milvus 向量 + PostgreSQL BM25 + RRF 重排序）。

    Example:
        >>> # 快速使用内存模式（开发测试）
        >>> from agent_framework.memory.bm25_retriever import BM25Retriever
        >>> r = BM25Retriever(source="memory")
        >>> r.add_documents(["退货政策：7天无理由退换货"])
        >>> agent = RagAgent(retriever=r, system_prompt="你是电商客服。")
        >>> print(agent.chat("怎么退货？"))

        >>> # 生产使用混合检索
        >>> from agent_framework.memory.hybrid_retriever import HybridRetriever
        >>> from agent_framework.memory.vector_store import MilvusRetriever
        >>> from agent_framework.memory.bm25_retriever import BM25Retriever
        >>> retriever = HybridRetriever(
        ...     vector_retriever=MilvusRetriever(collection_name="kb"),
        ...     bm25_retriever=BM25Retriever(source="postgresql", collection="kb"),
        ... )
        >>> agent = RagAgent(retriever=retriever, system_prompt="你是电商客服。")
    """

    def __init__(
        self,
        retriever: Optional[BaseRetriever] = None,
        llm: Optional[BaseChatModel] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list[BaseTool]] = None,
        retrieve_k: int = 5,
        checkpointer_backend: str = "memory",
        session_id: str = "default",
        verbose: bool = False,
    ) -> None:
        """
        Args:
            retriever:            检索器实例（BaseRetriever 子类）。
                                  为 None 时默认创建 HybridRetriever。
            llm:                  ChatModel 实例（为 None 时使用配置默认模型）
            system_prompt:        系统提示词
            tools:                可选额外工具（注入 LLM 节点）
            retrieve_k:           每次检索返回的文档数量
            checkpointer_backend: 检查点后端 'memory' | 'sqlite' | 'redis'
            session_id:           会话 ID
            verbose:              是否打印调试信息
        """
        super().__init__(session_id=session_id, verbose=verbose)
        self._llm = llm or create_llm()
        self._system_prompt = system_prompt
        self._tools = tools
        self._retrieve_k = retrieve_k
        self._checkpointer = get_checkpointer(checkpointer_backend)

        # 懒加载默认检索器
        if retriever is None:
            from agent_framework.memory.hybrid_retriever import HybridRetriever
            retriever = HybridRetriever()
        self._retriever = retriever

    def build_graph(self) -> CompiledStateGraph:
        """构建 RAG 图。"""
        return build_rag_agent(
            llm=self._llm,
            retriever=self._retriever,
            system_prompt=self._system_prompt,
            tools=self._tools,
            retrieve_k=self._retrieve_k,
            checkpointer=self._checkpointer,
        )
