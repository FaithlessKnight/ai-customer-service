"""
RRF 混合检索模块

将 Milvus 向量检索和 BM25 关键词检索的结果通过
Reciprocal Rank Fusion（RRF）算法融合重排序。

RRF 公式:
    score(d) = w_v * 1/(k + rank_v(d))  +  w_b * 1/(k + rank_b(d))

依赖:
    pip install pymilvus langchain-milvus jieba rank_bm25
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Optional

from agent_framework.memory.base_retriever import BaseRetriever, RetrievedChunk

logger = logging.getLogger(__name__)


class HybridRetriever(BaseRetriever):
    """
    混合检索器：Milvus 向量检索 + BM25 关键词检索 + RRF 重排序。

    Example:
        >>> from agent_framework.memory.vector_store import MilvusRetriever
        >>> from agent_framework.memory.bm25_retriever import BM25Retriever
        >>> retriever = HybridRetriever(
        ...     vector_retriever=MilvusRetriever(collection_name="kb"),
        ...     bm25_retriever=BM25Retriever(source="postgresql", collection="kb"),
        ... )
        >>> retriever.add_documents(["退货政策：7天无理由退换货"])
        >>> results = retriever.retrieve("怎么退货", k=5)
    """

    def __init__(
        self,
        vector_retriever: Optional[BaseRetriever] = None,
        bm25_retriever: Optional[BaseRetriever] = None,
        rrf_k: Optional[int] = None,
        vector_weight: Optional[float] = None,
        bm25_weight: Optional[float] = None,
        oversampling: int = 3,
    ) -> None:
        """
        Args:
            vector_retriever: 向量检索器（为 None 时默认创建 MilvusRetriever）
            bm25_retriever:   BM25 检索器（为 None 时默认创建内存模式）
            rrf_k:            RRF 平滑常数（默认读取配置 rrf_k=60）
            vector_weight:    向量结果加权系数（默认读取配置 hybrid_vector_weight=0.6）
            bm25_weight:      BM25 结果加权系数（默认读取配置 hybrid_bm25_weight=0.4）
            oversampling:     各路召回 k*oversampling 条后再融合，提高召回覆盖
        """
        from agent_framework.config.settings import get_settings
        s = get_settings()

        if vector_retriever is None:
            from agent_framework.memory.vector_store import MilvusRetriever
            vector_retriever = MilvusRetriever()
        if bm25_retriever is None:
            from agent_framework.memory.bm25_retriever import BM25Retriever
            bm25_retriever = BM25Retriever(source="memory")

        self._vector = vector_retriever
        self._bm25 = bm25_retriever
        self._rrf_k = rrf_k if rrf_k is not None else s.rrf_k
        self._vector_weight = vector_weight if vector_weight is not None else s.hybrid_vector_weight
        self._bm25_weight = bm25_weight if bm25_weight is not None else s.hybrid_bm25_weight
        self._oversampling = max(1, oversampling if oversampling is not None else s.hybrid_oversampling)
        self._vector_timeout_ms = s.hybrid_vector_timeout_ms
        self._bm25_timeout_ms = s.hybrid_bm25_timeout_ms

    # ── RRF 核心算法 ──────────────────────────────────────
    def _rrf_fuse(
        self,
        vector_results: list[RetrievedChunk],
        bm25_results: list[RetrievedChunk],
        k: int,
    ) -> list[RetrievedChunk]:
        """
        加权 RRF 融合两路检索结果。

        以 content 字符串作为文档去重 key。
        后续对接文档 ID 后可替换为 metadata['doc_id']。
        """
        rrf_scores: dict[str, float] = {}
        chunk_map: dict[str, RetrievedChunk] = {}

        for rank, chunk in enumerate(vector_results, start=1):
            key = chunk.content
            rrf_scores[key] = rrf_scores.get(key, 0.0) + \
                self._vector_weight / (self._rrf_k + rank)
            chunk_map[key] = chunk

        for rank, chunk in enumerate(bm25_results, start=1):
            key = chunk.content
            rrf_scores[key] = rrf_scores.get(key, 0.0) + \
                self._bm25_weight / (self._rrf_k + rank)
            chunk_map.setdefault(key, chunk)

        sorted_keys = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)[:k]
        return [
            RetrievedChunk(
                content=chunk_map[key].content,
                score=rrf_scores[key],
                metadata=chunk_map[key].metadata,
                source="hybrid",
            )
            for key in sorted_keys
        ]

    # ── BaseRetriever 接口实现 ─────────────────────────────
    def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: Optional[dict[str, Any]] = None,
    ) -> list[RetrievedChunk]:
        """
        混合检索：向量 + BM25，经 RRF 融合后返回 top-k。

        两路并发查询 + 超时降级：
        - 向量超时后忽略向量结果
        - BM25 超时后忽略 BM25 结果
        任意一路可用都能返回结果，不抛出异常。

        Args:
            filters: metadata 过滤条件，透传两路检索器
        """
        fetch_k = k * self._oversampling
        vector_results: list[RetrievedChunk] = []
        bm25_results: list[RetrievedChunk] = []

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_vec = ex.submit(self._vector.retrieve, query, fetch_k, filters)
            f_bm = ex.submit(self._bm25.retrieve, query, fetch_k, filters)

            try:
                vector_results = f_vec.result(timeout=self._vector_timeout_ms / 1000)
            except FuturesTimeoutError:
                logger.warning("向量检索超时（%dms），降级为 BM25", self._vector_timeout_ms)
            except Exception as e:
                logger.warning("向量检索失败，降级为纯 BM25: %s", e)

            try:
                bm25_results = f_bm.result(timeout=self._bm25_timeout_ms / 1000)
            except FuturesTimeoutError:
                logger.warning("BM25 检索超时（%dms），降级为向量", self._bm25_timeout_ms)
            except Exception as e:
                logger.warning("BM25 检索失败，降级为纯向量: %s", e)

        if not vector_results and not bm25_results:
            return []
        if not vector_results:
            return bm25_results[:k]
        if not bm25_results:
            return vector_results[:k]

        return self._rrf_fuse(vector_results, bm25_results, k=k)

    def add_documents(
        self,
        texts: list[str],
        metadatas: Optional[list[dict[str, Any]]] = None,
    ) -> list[str]:
        """
        同时向两路检索器写入文档。
        返回向量检索器的 ID 列表（以 Milvus 主键为准）。
        """
        metas = metadatas or [{} for _ in texts]
        vector_ids: list[str] = []
        try:
            vector_ids = self._vector.add_documents(texts, metas)
        except Exception as e:
            logger.warning("向量写入失败: %s", e)

        try:
            self._bm25.add_documents(texts, metas)
        except Exception as e:
            logger.warning("BM25 写入失败: %s", e)

        return vector_ids

    def __repr__(self) -> str:
        return (
            f"HybridRetriever("
            f"rrf_k={self._rrf_k}, "
            f"vector_weight={self._vector_weight}, "
            f"bm25_weight={self._bm25_weight})"
        )
