"""
BM25 检索模块

使用 jieba 中文分词 + BM25 算法实现关键词全文检索。

数据来源支持两种模式（通过 source 参数切换）:
  - 'memory':     文档存储在内存中（开发/测试用）
  - 'postgresql': 从 PostgreSQL 读取文档，写入也同步到 PG（生产用）

后续对接 PostgreSQL 步骤:
    1. 确保 pg_client.py 中的 PostgresClient 能正常连接
    2. 调用 BM25Retriever(source='postgresql') 即可自动从 PG 加载文档
    3. add_documents() 会同时写入内存索引和 PG

依赖:
    pip install jieba rank_bm25
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from agent_framework.memory.base_retriever import BaseRetriever, RetrievedChunk, match_filters

logger = logging.getLogger(__name__)


class BM25Retriever(BaseRetriever):
    """
    BM25 关键词检索器（中文 jieba 分词）。

    内存模式（默认，开发/测试）:
        文档存储在进程内存，无需数据库连接。

    PostgreSQL 模式（生产）:
        add_documents() 同步写入 PG documents 表；
        retrieve() 先从 PG 加载文档分词结果，再做 BM25 排序。

    Example:
        >>> r = BM25Retriever(source="memory")
        >>> r.add_documents(["退货政策：7天无理由退换货", "运费满99包邮"])
        >>> results = r.retrieve("怎么退货", k=3)
    """

    def __init__(
        self,
        source: Literal["memory", "postgresql"] = "memory",
        collection: str = "default",
        k1: Optional[float] = None,
        b: Optional[float] = None,
    ) -> None:
        from agent_framework.config.settings import get_settings
        s = get_settings()
        self.source = source
        self.collection = collection
        self._k1 = k1 if k1 is not None else s.bm25_k1
        self._b = b if b is not None else s.bm25_b

        # 内存索引
        self._corpus_texts: list[str] = []
        self._corpus_tokens: list[list[str]] = []
        self._metadatas: list[dict] = []
        self._bm25 = None  # rank_bm25.BM25Okapi，文档变更后需重建

        self._init_jieba(s.jieba_user_dict)

    # ── jieba 初始化 ─────────────────────────────────────
    @staticmethod
    def _init_jieba(user_dict: str = "") -> None:
        """初始化 jieba，可选加载自定义词典。"""
        try:
            import jieba
            jieba.setLogLevel(logging.WARNING)
            if user_dict:
                import os
                if os.path.exists(user_dict):
                    jieba.load_userdict(user_dict)
                    logger.info("jieba 已加载自定义词典: %s", user_dict)
                else:
                    logger.warning("jieba 自定义词典不存在: %s", user_dict)
        except ImportError:
            raise ImportError("请安装: pip install jieba")

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """jieba 精确模式分词，过滤空白词。"""
        import jieba
        return [t for t in jieba.cut(text, cut_all=False) if t.strip()]

    # ── BM25 索引构建 ────────────────────────────────────
    def _rebuild_index(self) -> None:
        """根据当前语料重建 BM25 索引。"""
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            raise ImportError("请安装: pip install rank_bm25")
        if not self._corpus_tokens:
            self._bm25 = None
            return
        self._bm25 = BM25Okapi(
            self._corpus_tokens,
            k1=self._k1,
            b=self._b,
        )

    # ── PostgreSQL 对接预留 ──────────────────────────────
    def _pg_insert(self, texts: list[str], tokens: list[list[str]], metadatas: list[dict]) -> list[str]:
        """
        将文档写入 PostgreSQL documents 表。
        """
        from agent_framework.memory.pg_client import get_pg_client
        import json
        client = get_pg_client()
        ids = []
        with client.cursor() as cur:
            for text, tok, meta in zip(texts, tokens, metadatas):
                cur.execute(
                    "INSERT INTO documents(collection, content, tokens, metadata) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (self.collection, text, tok, json.dumps(meta, ensure_ascii=False))
                )
                ids.append(str(cur.fetchone()[0]))
        logger.info("[pg_insert] 写入 %d 条文档到 PG collection=%s", len(ids), self.collection)
        return ids

    def _pg_load_corpus(self) -> None:
        """
        从 PostgreSQL 加载文档到内存索引。
        """
        from agent_framework.memory.pg_client import get_pg_client
        client = get_pg_client()
        with client.cursor() as cur:
            cur.execute(
                "SELECT content, tokens, metadata FROM documents WHERE collection=%s",
                (self.collection,)
            )
            rows = cur.fetchall()
        self._corpus_texts = [r[0] for r in rows]
        self._corpus_tokens = [list(r[1]) for r in rows]
        self._metadatas = [r[2] for r in rows]
        self._rebuild_index()
        logger.info("[pg_load] 从 PG 加载 %d 条文档 collection=%s", len(rows), self.collection)

    # ── BaseRetriever 接口实现 ────────────────────────────
    def add_documents(
        self,
        texts: list[str],
        metadatas: Optional[list[dict[str, Any]]] = None,
    ) -> list[str]:
        """
        添加文档到 BM25 索引（同时预留写入 PG）。

        Args:
            texts:     文本列表
            metadatas: 可选元数据列表

        Returns:
            文档 ID 列表
        """
        metas = metadatas or [{} for _ in texts]
        tokens = [self._tokenize(t) for t in texts]

        # 更新内存索引
        self._corpus_texts.extend(texts)
        self._corpus_tokens.extend(tokens)
        self._metadatas.extend(metas)
        self._rebuild_index()

        # PostgreSQL 写入（预留）
        if self.source == "postgresql":
            return self._pg_insert(texts, tokens, metas)

        return [str(len(self._corpus_texts) - len(texts) + i) for i in range(len(texts))]

    def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: Optional[dict[str, Any]] = None,
    ) -> list[RetrievedChunk]:
        """
        BM25 关键词检索。

        Args:
            query:   查询文本（自动 jieba 分词）
            k:       返回数量
            filters: metadata 过滤；仅对满足过滤条件的候选文档计算 BM25 分数

        Returns:
            RetrievedChunk 列表（按 BM25 分数降序）
        """
        # PostgreSQL 模式：索引为空时才从 PG 加载（懒加载，非每次都加载）
        if self.source == "postgresql" and not self._corpus_tokens:
            try:
                self._pg_load_corpus()
            except Exception as e:
                logger.warning("[bm25] PG 语料加载失败，返回空结果: %s", e)
                return []

        if not self._corpus_tokens or self._bm25 is None:
            logger.debug("BM25 索引为空，返回空结果")
            return []

        try:
            query_tokens = self._tokenize(query)
            if not query_tokens:
                return []

            scores = self._bm25.get_scores(query_tokens)

            # 候选索引：filters 命中即保留；无 filters 全保留
            if filters:
                candidate_idx = [
                    i for i, m in enumerate(self._metadatas)
                    if match_filters(m or {}, filters)
                ]
                if not candidate_idx:
                    return []
                top_k_indices = sorted(candidate_idx, key=lambda i: scores[i], reverse=True)[:k]
            else:
                top_k_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]

            results = []
            for idx in top_k_indices:
                if scores[idx] <= 0:
                    continue
                results.append(RetrievedChunk(
                    content=self._corpus_texts[idx],
                    score=float(scores[idx]),
                    metadata=self._metadatas[idx],
                    source="bm25",
                ))
            return results
        except Exception as e:
            logger.warning("[bm25] 检索失败，返回空结果: %s", e)
            return []

    def __repr__(self) -> str:
        return (
            f"BM25Retriever(source={self.source!r}, "
            f"collection={self.collection!r}, "
            f"docs={len(self._corpus_texts)})"
        )
