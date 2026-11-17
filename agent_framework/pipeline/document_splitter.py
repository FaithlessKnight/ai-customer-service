"""
文档分块模块（电商场景优化）

分块策略优先级：
  1. 语义段落优先：按标题 / 空行识别自然段落边界
  2. 字数兜底：段落仍超长时按句子边界滑动切分
  3. 重叠窗口：相邻块保留 overlap 字符，减少上下文断裂

电商场景特化：
  - 识别「商品信息块」整体保留（含 SKU/价格/规格等关键字）
  - 识别「SOP 步骤块」保持步骤完整性
  - 表格行不拆分，整表作为一个块
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from agent_framework.pipeline.base_cleaner import CleanedDocument

logger = logging.getLogger(__name__)

# ── 电商场景正则 ──────────────────────────────────────────────
_PRODUCT_KW_RE = re.compile(
    r"(SKU|sku|型号|规格|颜色|尺寸|重量|价格|库存|分类|品牌|产品名|商品名|货号)"
)
_SOP_STEP_RE = re.compile(
    r"^(第[一二三四五六七八九十百\d]+步|步骤\s*\d+|\d+[.、]\s)",
    re.MULTILINE,
)
_TABLE_ROW_RE = re.compile(r"(?:[^\n]*\|[^\n]*\n){2,}")
_HEADING_RE = re.compile(r"^(#{1,6}\s+|【.{1,20}】)", re.MULTILINE)
_SENT_END_RE = re.compile(r'[\u3002\uff01\uff1f!?\u2026]+[\u201d\u300d\u300f]?')


@dataclass
class DocumentChunk:
    """
    分块后的文档片段，可直接传给 HybridRetriever.add_documents()。

    Attributes:
        content:    分块文本内容
        metadata:   继承自 CleanedDocument 并新增分块信息
        chunk_id:   块唯一标识（doc_id_chunkN）
        chunk_type: paragraph / product / sop_step / table
    """
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    chunk_id: str = ""
    chunk_type: str = "paragraph"


class DocumentSplitter:
    """
    电商场景文档分块器。

    Example:
        >>> from agent_framework.pipeline.base_cleaner import CleanedDocument
        >>> splitter = DocumentSplitter(chunk_size=512, overlap=64)
        >>> chunks = splitter.split(cleaned_docs)
    """

    def __init__(
        self,
        chunk_size: int = 512,
        overlap: int = 64,
        min_chunk_size: int = 30,
    ) -> None:
        """
        Args:
            chunk_size:     单块最大字符数
            overlap:        相邻块重叠字符数（减少跨块语义截断）
            min_chunk_size: 低于此长度的块直接丢弃（避免碎片噪声）
        """
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.min_chunk_size = min_chunk_size

    # ── 公共接口 ──────────────────────────────────────────────
    def split(self, documents: list[CleanedDocument]) -> list[DocumentChunk]:
        """
        对 CleanedDocument 列表分块。

        Args:
            documents: TextCleaner 输出的清洗文档列表

        Returns:
            DocumentChunk 列表，可直接传入 HybridRetriever.add_documents()
        """
        all_chunks: list[DocumentChunk] = []
        for doc in documents:
            all_chunks.extend(self._split_one(doc))
        logger.info(
            "分块完成：%d 个文档 -> %d 个块（chunk_size=%d, overlap=%d）",
            len(documents), len(all_chunks), self.chunk_size, self.overlap,
        )
        return all_chunks

    # ── 单文档处理 ────────────────────────────────────────────
    def _split_one(self, doc: CleanedDocument) -> list[DocumentChunk]:
        """对单个文档按结构分段，超长段落再滑动切分。"""
        segments = self._segment(doc.content)
        chunks: list[DocumentChunk] = []
        idx = 0
        for seg_text, seg_type in segments:
            seg_text = seg_text.strip()
            if not seg_text:
                continue
            if len(seg_text) <= self.chunk_size:
                if len(seg_text) >= self.min_chunk_size:
                    chunks.append(self._make_chunk(seg_text, doc, idx, seg_type))
                    idx += 1
            else:
                for sub in self._sliding_split(seg_text):
                    if len(sub) >= self.min_chunk_size:
                        chunks.append(self._make_chunk(sub, doc, idx, seg_type))
                        idx += 1
        return chunks

    # ── 结构化段落识别 ────────────────────────────────────────
    def _segment(self, text: str) -> list[tuple[str, str]]:
        """
        将文本拆分为 (内容, 类型) 段落列表。
        类型: paragraph / product / sop_step / table
        """
        results: list[tuple[str, str]] = []

        # 1. 提取表格块（整体保留）
        remaining = text
        for m in _TABLE_ROW_RE.finditer(text):
            results.append((m.group(0).strip(), "table"))
        remaining = _TABLE_ROW_RE.sub("\x00TABLE\x00", remaining)

        # 2. 按标题或双空行切分段落
        raw_blocks = re.split(r"\n{2}|(?=^#{1,6}\s)|(?=^【)", remaining, flags=re.MULTILINE)

        for block in raw_blocks:
            block = block.replace("\x00TABLE\x00", "").strip()
            if not block:
                continue
            chunk_type = self._classify(block)
            results.append((block, chunk_type))

        return results

    def _classify(self, text: str) -> str:
        """根据内容关键词判断块类型。"""
        if _SOP_STEP_RE.search(text):
            return "sop_step"
        if _PRODUCT_KW_RE.search(text):
            return "product"
        return "paragraph"

    # ── 滑动窗口切分 ──────────────────────────────────────────
    def _sliding_split(self, text: str) -> list[str]:
        """
        对超长段落按句子边界做滑动窗口切分。
        优先在句末标点处断开，保证语义完整。
        """
        sentences = self._split_sentences(text)
        chunks: list[str] = []
        buf = ""

        for sent in sentences:
            if len(buf) + len(sent) <= self.chunk_size:
                buf += sent
            else:
                if buf:
                    chunks.append(buf.strip())
                # 新块从上一块末尾取 overlap 字符开头
                overlap_text = buf[-self.overlap:] if len(buf) > self.overlap else buf
                buf = overlap_text + sent

        if buf.strip():
            chunks.append(buf.strip())

        return chunks if chunks else [text[: self.chunk_size]]

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """按中英文句末标点切分句子，保留标点。"""
        parts = _SENT_END_RE.split(text)
        seps = _SENT_END_RE.findall(text)
        sentences: list[str] = []
        for i, part in enumerate(parts):
            sent = part + (seps[i] if i < len(seps) else "")
            if sent.strip():
                sentences.append(sent)
        return sentences

    # ── 构建 DocumentChunk ────────────────────────────────────
    def _make_chunk(
        self,
        text: str,
        doc: CleanedDocument,
        idx: int,
        chunk_type: str,
    ) -> DocumentChunk:
        """构建带完整元数据的 DocumentChunk。"""
        chunk_id = f"{doc.doc_id}_chunk{idx}"
        meta = {
            **doc.metadata,
            "chunk_id": chunk_id,
            "chunk_index": idx,
            "chunk_type": chunk_type,
            "char_count": len(text),
            "doc_id": doc.doc_id,
        }
        return DocumentChunk(
            content=text,
            metadata=meta,
            chunk_id=chunk_id,
            chunk_type=chunk_type,
        )
