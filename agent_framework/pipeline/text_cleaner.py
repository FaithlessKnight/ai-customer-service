"""
文本清洗模块

提供多个单职责清洗器，可按需组合：
  - WhitespaceCleaner:    空白符/控制字符规范化
  - UnicodeNormalizer:    全半角统一、NFC 规范化
  - SimplifiedConverter:  繁体 -> 简体中文
  - LengthFilter:         过滤过短/过长文本
  - DedupCleaner:         MD5 去重
  - TextCleaner:          以上步骤的组合（推荐直接使用）

依赖:
    pip install opencc-python-reimplemented
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from typing import Any

from agent_framework.pipeline.base_cleaner import BaseCleaner, CleanedDocument

logger = logging.getLogger(__name__)


class WhitespaceCleaner(BaseCleaner):
    """清洗控制字符、规范化空白符和连续空行。"""

    _CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
    _MULTI_NL_RE = re.compile(r"\n{3,}")
    _MULTI_SP_RE = re.compile(r"[ \t]{2,}")

    def clean_one(self, text: str, metadata: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        text = self._CTRL_RE.sub("", text)
        text = self._MULTI_SP_RE.sub(" ", text)
        text = self._MULTI_NL_RE.sub("\n\n", text)
        return text.strip(), metadata


class UnicodeNormalizer(BaseCleaner):
    """NFC 规范化 + 全角字母数字转半角 + 全角空格转半角。"""

    _FULL_CHARS_RE = re.compile(r"[\uFF01-\uFF5E]")

    def clean_one(self, text: str, metadata: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        text = unicodedata.normalize("NFC", text)
        text = text.replace("\u3000", " ")
        text = self._FULL_CHARS_RE.sub(lambda m: chr(ord(m.group(0)) - 0xFEE0), text)
        return text, metadata


class SimplifiedConverter(BaseCleaner):
    """
    繁体中文 -> 简体中文。
    依赖 opencc-python-reimplemented；未安装时直接透传，不报错。
    """

    def __init__(self) -> None:
        self._converter = None
        try:
            import opencc
            self._converter = opencc.OpenCC("t2s")
        except ImportError:
            logger.warning("opencc 未安装，繁简转换已跳过。pip install opencc-python-reimplemented")

    def clean_one(self, text: str, metadata: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if self._converter:
            text = self._converter.convert(text)
        return text, metadata


class LengthFilter(BaseCleaner):
    """
    按字符数过滤文本块。
    - 低于 min_length：视为噪声丢弃
    - 高于 max_length：截断并记录警告（分块器会进一步拆分）
    """

    def __init__(self, min_length: int = 10, max_length: int = 10000) -> None:
        self.min_length = min_length
        self.max_length = max_length

    def clean_one(self, text: str, metadata: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        length = len(text)
        if length < self.min_length:
            logger.debug("丢弃过短文本（%d 字符）", length)
            return "", metadata
        if length > self.max_length:
            logger.warning("文本过长（%d 字符），将截断至 %d", length, self.max_length)
            text = text[: self.max_length]
        return text, metadata


class DedupCleaner(BaseCleaner):
    """
    基于内容 MD5 的原地去重。
    同一 DedupCleaner 实例内，重复内容只保留首次出现。
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def clean_one(self, text: str, metadata: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        digest = hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()
        if digest in self._seen:
            logger.debug("去重：跳过重复内容（md5=%s）", digest[:8])
            return "", metadata
        self._seen.add(digest)
        return text, metadata

    def reset(self) -> None:
        """清空去重缓存（新一批次入库前调用）。"""
        self._seen.clear()


class EcommerceNormalizer(BaseCleaner):
    """
    电商场景专用规范化：
    - 统一价格格式：￥/¥ → 元
    - 清理多余的促销符号（★☆●○◆◇）
    - 规范 SKU / 型号中的分隔符
    """

    _PRICE_RE = re.compile(r"[￥¥](\d)")
    _PROMO_RE = re.compile(r"[★☆●○◆◇►▶]+")
    _SKU_SEP_RE = re.compile(r"[／/]")

    def clean_one(self, text: str, metadata: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        text = self._PRICE_RE.sub(r"\1 元", text)
        text = self._PROMO_RE.sub(" ", text)
        text = self._SKU_SEP_RE.sub("/", text)
        return text, metadata


class TextCleaner:
    """
    组合清洗器（推荐直接使用）。

    按顺序执行：
        1. WhitespaceCleaner    — 去控制字符
        2. UnicodeNormalizer    — 全半角统一
        3. SimplifiedConverter  — 繁 -> 简
        4. EcommerceNormalizer  — 电商特化
        5. LengthFilter         — 长度过滤
        6. DedupCleaner         — 去重

    Example:
        >>> cleaner = TextCleaner(min_length=15)
        >>> docs = cleaner.clean(["退貨政策：７天無理由退換貨"], [{"source": "sop.docx"}])
        >>> docs[0].content
        '退货政策：7天无理由退换货'
    """

    def __init__(
        self,
        min_length: int = 10,
        max_length: int = 10000,
        enable_simplified: bool = True,
        enable_dedup: bool = True,
    ) -> None:
        self._steps: list[BaseCleaner] = [
            WhitespaceCleaner(),
            UnicodeNormalizer(),
        ]
        if enable_simplified:
            self._steps.append(SimplifiedConverter())
        self._steps.extend([
            EcommerceNormalizer(),
            LengthFilter(min_length=min_length, max_length=max_length),
        ])
        self._dedup = DedupCleaner() if enable_dedup else None
        if self._dedup:
            self._steps.append(self._dedup)

    def clean(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> list[CleanedDocument]:
        """
        对文本列表依次执行全部清洗步骤。

        Args:
            texts:     原始文本列表
            metadatas: 可选元数据列表

        Returns:
            清洗后的 CleanedDocument 列表
        """
        metas = metadatas or [{} for _ in texts]
        current_texts = list(texts)
        current_metas = list(metas)

        for step in self._steps:
            next_texts: list[str] = []
            next_metas: list[dict] = []
            for text, meta in zip(current_texts, current_metas):
                cleaned, updated_meta = step.clean_one(text, meta)
                next_texts.append(cleaned)
                next_metas.append(updated_meta)
            current_texts = next_texts
            current_metas = next_metas

        results: list[CleanedDocument] = []
        for text, meta in zip(current_texts, current_metas):
            if text.strip():
                results.append(CleanedDocument(content=text, metadata=meta))

        logger.info("文本清洗完成：输入 %d 条 -> 输出 %d 条", len(texts), len(results))
        return results

    def reset_dedup(self) -> None:
        """重置去重缓存（新批次入库前调用）。"""
        if self._dedup:
            self._dedup.reset()
