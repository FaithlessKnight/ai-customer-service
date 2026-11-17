"""
数据入库管道（Ingest Pipeline）

编排完整的数据处理流程：
    加载(Loader) -> 清洗(TextCleaner) -> 分块(DocumentSplitter) -> 入库(Retriever)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent_framework.pipeline.base_cleaner import CleanedDocument
from agent_framework.pipeline.document_splitter import DocumentChunk, DocumentSplitter
from agent_framework.pipeline.loaders.base_loader import LoadedDocument
from agent_framework.pipeline.loaders.file_loader import FileLoader
from agent_framework.pipeline.text_cleaner import TextCleaner

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """入库结果摘要。"""
    total_files: int = 0
    total_docs: int = 0
    cleaned_docs: int = 0
    total_chunks: int = 0
    ingested_ids: list[str] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return len(self.ingested_ids)

    def summary(self) -> str:
        return (
            f"入库完成 | 文件:{self.total_files} | "
            f"原始块:{self.total_docs} | 清洗后:{self.cleaned_docs} | "
            f"分块:{self.total_chunks} | 写入:{self.success_count} | "
            f"跳过:{self.skipped} | 错误:{len(self.errors)}"
        )


class IngestPipeline:
    """
    数据入库管道。

    Example:
        >>> from agent_framework.memory.hybrid_retriever import HybridRetriever
        >>> pipeline = IngestPipeline(retriever=HybridRetriever())
        >>> result = pipeline.ingest_files(["products.xlsx", "sop.docx"])
        >>> print(result.summary())
    """

    def __init__(
        self,
        retriever=None,
        loader: Optional[FileLoader] = None,
        cleaner: Optional[TextCleaner] = None,
        splitter: Optional[DocumentSplitter] = None,
        batch_size: int = 64,
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        from agent_framework.config.settings import get_settings
        s = get_settings()

        if retriever is None:
            from agent_framework.memory.bm25_retriever import BM25Retriever
            retriever = BM25Retriever(source="memory")
            logger.warning("未指定 retriever，使用内存 BM25（仅测试用）")

        self._retriever = retriever
        self._loader = loader or FileLoader()
        self._cleaner = cleaner or TextCleaner(
            min_length=s.pipeline_min_chunk_length,
            max_length=s.pipeline_max_chunk_length,
        )
        self._splitter = splitter or DocumentSplitter(
            chunk_size=s.pipeline_chunk_size,
            overlap=s.pipeline_chunk_overlap,
            min_chunk_size=s.pipeline_min_chunk_length,
        )
        self._batch_size = batch_size
        self._extra_meta = extra_metadata or {}

        # 词典自动提取器
        from agent_framework.config.settings import get_settings
        from agent_framework.pipeline.dict_extractor import get_extractor
        _s = get_settings()
        self._dict_extractor = get_extractor(
            dict_path=_s.jieba_user_dict or "data/dicts/custom.txt"
        )

    # ── 主入口：文件列表 ──────────────────────────────────
    def ingest_files(
        self,
        file_paths: list[str | Path],
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> IngestResult:
        """
        批量处理本地文件并入库。
        支持 txt/md/csv/json/jsonl/pdf/docx/xlsx/xls。
        """
        result = IngestResult(total_files=len(file_paths))
        meta_override = {**self._extra_meta, **(extra_metadata or {})}

        # 每次入库前重置去重缓存，避免跨批次误删
        self._cleaner.reset_dedup()

        # 1. 加载
        loaded_docs: list[LoadedDocument] = []
        for fp in file_paths:
            try:
                docs = self._loader.load(fp)
                loaded_docs.extend(docs)
                logger.info("加载: %s -> %d 块", Path(fp).name, len(docs))
            except Exception as e:
                msg = f"加载失败 [{fp}]: {e}"
                logger.error(msg)

        result.total_docs = len(loaded_docs)
        if not loaded_docs:
            return result

        texts = [d.content for d in loaded_docs]
        metas = [{**d.metadata, **meta_override} for d in loaded_docs]

        # 2. 清洗
        cleaned = self._cleaner.clean(texts, metas)
        result.cleaned_docs = len(cleaned)

        # 3. 分块
        chunks = self._splitter.split(cleaned)
        result.total_chunks = len(chunks)

        # 4. 入库
        self._ingest_chunks(chunks, result, source_texts=texts)
        logger.info(result.summary())
        return result

    # ── 直接传入文本入库 ──────────────────────────────────
    def ingest_texts(
        self,
        texts: list[str],
        metadatas: Optional[list[dict[str, Any]]] = None,
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> IngestResult:
        """
        直接传入文本列表入库（不经过 Loader）。
        适合从已有数据库读取内容后传入。
        """
        result = IngestResult(total_files=0)
        meta_override = {**self._extra_meta, **(extra_metadata or {})}
        metas = [{**( m or {}), **meta_override} for m in (metadatas or [{} for _ in texts])]

        # 每次入库前重置去重缓存
        self._cleaner.reset_dedup()
        result.total_docs = len(texts)
        cleaned = self._cleaner.clean(texts, metas)
        result.cleaned_docs = len(cleaned)
        chunks = self._splitter.split(cleaned)
        result.total_chunks = len(chunks)
        self._ingest_chunks(chunks, result, source_texts=texts)
        logger.info(result.summary())
        return result

    # ── 批量写入检索库 ────────────────────────────────────
    def _ingest_chunks(
        self, chunks: list[DocumentChunk], result: IngestResult,
        source_texts: Optional[list[str]] = None,
    ) -> None:
        """将 DocumentChunk 列表分批写入检索器，写入完成后自动更新词典。"""
        for i in range(0, len(chunks), self._batch_size):
            batch = chunks[i: i + self._batch_size]
            texts = [c.content for c in batch]
            metas = [c.metadata for c in batch]
            # 确保所有 chunk 都携带 product_name 字段（非商品类 collection 可为空字符串），
            # 避免 Milvus schema 中 product_name 为必填时写入失败
            for m in metas:
                m.setdefault("product_name", "")
            try:
                ids = self._retriever.add_documents(texts, metas)
                result.ingested_ids.extend(ids)
                logger.debug("批次 %d-%d 写入完成", i, i + len(batch))
            except Exception as e:
                msg = f"写入失败（批次 {i}-{i + len(batch)}）: {e}"
                logger.error(msg)

        # 入库完成后自动提取新词并热加载到 jieba
        texts_for_extract = source_texts or [c.content for c in chunks]
        if texts_for_extract:
            try:
                new_words = self._dict_extractor.extract_and_update(texts_for_extract)
                if new_words:
                    logger.info("[词典] 本次入库新增词条: %s", "、".join(new_words))
            except Exception as e:
                logger.warning("[词典] 自动提取失败（不影响入库）: %s", e)

    # ── 预览（不入库，仅返回分块结果） ────────────────────
    def preview(
        self,
        file_path: str | Path,
        max_chunks: int = 10,
    ) -> list[DocumentChunk]:
        """
        预览文件的清洗和分块结果，不写入任何数据库。
        便于调试分块效果。

        Args:
            file_path:  单个文件路径
            max_chunks: 最多返回的块数

        Returns:
            DocumentChunk 列表
        """
        loaded = self._loader.load(file_path)
        texts = [d.content for d in loaded]
        metas = [d.metadata for d in loaded]
        cleaned = self._cleaner.clean(texts, metas)
        chunks = self._splitter.split(cleaned)
        return chunks[:max_chunks]
