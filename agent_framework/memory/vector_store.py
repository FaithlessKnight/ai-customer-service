"""
Milvus 向量检索模块

基于 Milvus + OpenAI Embeddings 实现语义向量检索。
实现 BaseRetriever 接口，可与 BM25Retriever 组合成混合检索。

后续对接步骤:
    1. 部署 Milvus（Docker: docker run -d milvusdb/milvus:latest）
    2. 在 .env 中配置 MILVUS_HOST / MILVUS_PORT 等
    3. 直接实例化 MilvusRetriever 即可使用

依赖:
    pip install pymilvus langchain-milvus
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from langchain_core.documents import Document

from agent_framework.memory.base_retriever import BaseRetriever, RetrievedChunk, build_milvus_expr

logger = logging.getLogger(__name__)


class MilvusRetriever(BaseRetriever):
    """
    Milvus 向量检索器。

    懒加载连接，首次调用 retrieve() 或 add_documents() 时才真正建立连接。
    Collection 不存在时自动创建（包含向量索引）。

    Example:
        >>> retriever = MilvusRetriever(collection_name="knowledge_base")
        >>> retriever.add_documents(["退货政策：7天无理由退换货"])
        >>> results = retriever.retrieve("怎么退货", k=3)
    """

    def __init__(
        self,
        collection_name: Optional[str] = None,
        embedding_model: Optional[str] = None,
        embedding_dim: Optional[int] = None,
    ) -> None:
        """
        Args:
            collection_name: Milvus Collection 名称（默认读取配置）
            embedding_model: Embedding 模型名（默认读取配置）
            embedding_dim:   向量维度（默认读取配置）
        """
        from agent_framework.config.settings import get_settings
        s = get_settings()
        self.collection_name = collection_name or s.milvus_default_collection
        self._embedding_model = embedding_model or s.embedding_model
        self._embedding_dim = embedding_dim or s.embedding_dim
        self._store = None  # 懒加载

    def _get_store(self):
        if self._store is not None:
            return self._store
        try:
            from langchain_milvus import Milvus
            from pymilvus import connections, MilvusClient
            from agent_framework.config.settings import get_settings
            s = get_settings()

            uri = f"http://{s.milvus_host}:{s.milvus_port}"
            # 只有 user 和 password 都非空时才构造 token，避免生成 ":password" 无效格式
            if s.milvus_user and s.milvus_password:
                token = f"{s.milvus_user}:{s.milvus_password}"
            else:
                token = ""

            # 先用 MilvusClient 建立连接，获取内部 alias
            mc = MilvusClient(uri=uri, token=token)
            alias = mc._using
            # 同步注册到旧版 Collection API
            if not connections.has_connection(alias):
                connections.connect(alias=alias, uri=uri, token=token)
            logger.info("Milvus 连接已建立: uri=%s alias=%s", uri, alias)

            # Embedding 模型
            if s.embedding_provider == "ollama":
                from langchain_ollama import OllamaEmbeddings
                embeddings = OllamaEmbeddings(
                    model=self._embedding_model,
                    base_url=s.ollama_base_url,
                )
                logger.info("使用 Ollama Embedding: %s", self._embedding_model)
            else:
                from langchain_openai import OpenAIEmbeddings
                embeddings = OpenAIEmbeddings(
                    model=self._embedding_model,
                    api_key=s.effective_api_key,
                    base_url=s.openai_base_url,
                )
                logger.info("使用 OpenAI Embedding: %s", self._embedding_model)

            # ── 自动探测实际向量维度，防止配置与模型不匹配 ────────
            actual_dim = self._detect_embedding_dim(embeddings)
            if actual_dim and actual_dim != self._embedding_dim:
                logger.warning(
                    "[vector_store] Embedding 维度不匹配！"
                    "配置 EMBEDDING_DIM=%d，模型实际输出 %d 维。"
                    "已自动修正为 %d，请更新 .env 中的 EMBEDDING_DIM。",
                    self._embedding_dim, actual_dim, actual_dim,
                )
                self._embedding_dim = actual_dim
            elif actual_dim:
                logger.info(
                    "[vector_store] Embedding 维度验证通过: model=%s dim=%d",
                    self._embedding_model, actual_dim,
                )

            self._store = Milvus(
                embedding_function=embeddings,
                collection_name=self.collection_name,
                connection_args={"uri": uri, "token": token},
                auto_id=True,
                drop_old=False,
            )
            logger.info("Milvus 向量存储初始化完成: collection=%s dim=%d", self.collection_name, self._embedding_dim)
        except ImportError:
            raise ImportError("请安装: pip install pymilvus langchain-milvus langchain-ollama")
        return self._store

    def _detect_embedding_dim(self, embeddings) -> int | None:
        """
        用一个测试向量探测 Embedding 模型的实际输出维度。
        探测失败时返回 None，不影响正常初始化流程。
        """
        try:
            vec = embeddings.embed_query("dim_probe")
            return len(vec)
        except Exception as e:
            logger.warning("[vector_store] 无法探测 Embedding 维度（将使用配置值 %d）: %s", self._embedding_dim, e)
            return None

    # ── BaseRetriever 接口实现 ─────────────────────────────
    def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: Optional[dict[str, Any]] = None,
    ) -> list[RetrievedChunk]:
        """
        语义向量检索，返回与 query 最相似的 k 条文档。
        连接断开时自动重连重试一次。

        Args:
            query:   查询文本
            k:       返回数量
            filters: metadata 过滤；转成 Milvus boolean expression 后下推到引擎
                     （要求 metadata 字段在 Collection schema 中已 flatten 为 scalar）
        """
        expr = build_milvus_expr(filters)
        for attempt in range(2):
            try:
                store = self._get_store()
                kw = {"k": k}
                if expr:
                    kw["expr"] = expr
                docs_with_score: list[tuple[Document, float]] = (
                    store.similarity_search_with_score(query, **kw)
                )
                return [
                    RetrievedChunk(
                        content=doc.page_content,
                        score=float(score),
                        metadata=doc.metadata,
                        source="milvus",
                    )
                    for doc, score in docs_with_score
                ]
            except Exception as e:
                err_str = str(e).lower()
                is_closed = (
                    "closed channel" in err_str
                    or "cannot invoke rpc" in err_str
                    or "connectionnotexist" in err_str
                    or "should create connection" in err_str
                )
                if attempt == 0 and is_closed:
                    logger.warning("[vector_store] Milvus 连接异常，正在丢弃缓存并重连: %s", e)
                    self._store = None
                    continue
                raise

    def add_documents(
        self,
        texts: list[str],
        metadatas: Optional[list[dict[str, Any]]] = None,
    ) -> list[str]:
        """
        将文本插入 Milvus Collection。
        连接断开时自动重连重试一次。

        说明：langchain-milvus 的 ``add_texts`` 在部分环境下会对 Ollama 发起错误的 embed
        代理请求；``embedding_provider=ollama`` 时改为先 ``embed_documents`` 再 ``add_embeddings``。
        """
        from agent_framework.config.settings import get_settings

        metas = metadatas or [{} for _ in texts]
        s = get_settings()

        def _add_with_retry(fn):
            for attempt in range(2):
                try:
                    store = self._get_store()
                    return fn(store)
                except Exception as e:
                    err_str = str(e).lower()
                    is_closed = (
                        "closed channel" in err_str
                        or "cannot invoke rpc" in err_str
                        or "connectionnotexist" in err_str
                        or "should create connection" in err_str
                    )
                    if attempt == 0 and is_closed:
                        logger.warning("[vector_store] Milvus 连接异常，正在丢弃缓存并重连: %s", e)
                        self._store = None
                        continue
                    raise

        if s.embedding_provider == "ollama":

            def _ollama_add(store):
                # 较长文本批量 embed 时，langchain_ollama 可能触发异常端点；分批嵌入再一次性写入 Milvus
                embed_batch = max(1, s.ollama_embed_batch_size)
                vectors: list[list[float]] = []
                for i in range(0, len(texts), embed_batch):
                    chunk = texts[i : i + embed_batch]
                    vectors.extend(store.embeddings.embed_documents(chunk))
                return store.add_embeddings(
                    texts=texts, embeddings=vectors, metadatas=metas
                )

            return _add_with_retry(_ollama_add)

        def _default_add(store):
            return store.add_texts(texts=texts, metadatas=metas)

        return _add_with_retry(_default_add)

    def delete_collection(self) -> None:
        """删除整个 Collection（谨慎使用）。"""
        try:
            from pymilvus import utility
            utility.drop_collection(self.collection_name)
            self._store = None
            logger.warning("已删除 Milvus Collection: %s", self.collection_name)
        except ImportError:
            raise ImportError("请安装: pip install pymilvus")
