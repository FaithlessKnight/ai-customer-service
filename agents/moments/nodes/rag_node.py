"""
朋友圈评论话术检索节点（异步）

从 moments_knowledge 集合检索评论话术，供生成节点使用。
复用 HybridRetriever（Milvus 向量 + BM25），与客服 RAG 节点相同模式。
"""
from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache
from typing import Any

from agents.moments.state import MomentsState

logger = logging.getLogger(__name__)

_COLLECTION = "moments_knowledge"


@lru_cache(maxsize=1)
def _get_retriever():
    """懒加载 moments_knowledge 检索器，结果缓存。"""
    from agent_framework.memory.hybrid_retriever import HybridRetriever
    from agent_framework.memory.vector_store import MilvusRetriever
    from agent_framework.memory.bm25_retriever import BM25Retriever
    retriever = HybridRetriever(
        vector_retriever=MilvusRetriever(collection_name=_COLLECTION),
        bm25_retriever=BM25Retriever(source="postgresql", collection=_COLLECTION),
    )
    logger.info("[moments/rag] 检索器初始化完成: collection=%s", _COLLECTION)
    return retriever


async def moments_rag_node(state: MomentsState) -> dict[str, Any]:
    """
    朋友圈话术检索节点（异步）。

    输入:  moment_content
    输出:  retrieved_chunks, debug_info
    """
    t0             = time.time()
    moment_content = state.get("moment_content", "")

    chunks: list[dict[str, Any]] = []
    try:
        retriever = _get_retriever()
        loop      = asyncio.get_event_loop()
        results   = await loop.run_in_executor(
            None, lambda: retriever.retrieve(moment_content, k=5)
        )
        chunks = [
            {"content": r.content, "score": round(r.score, 4), "metadata": r.metadata}
            for r in results if r.score > 0
        ][:3]  # 评论只需最相关的 3 条话术
    except Exception as e:
        logger.warning("[moments/rag] 检索失败，继续生成: %s", e)

    debug_info = dict(state.get("debug_info") or {})
    debug_info["rag_ms"] = round((time.time() - t0) * 1000)

    logger.info("[moments/rag] hits=%d query=%r", len(chunks), moment_content[:40])
    return {
        "retrieved_chunks": chunks,
        "debug_info":       debug_info,
    }
