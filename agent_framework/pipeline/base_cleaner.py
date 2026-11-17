"""
清洗器抽象基类

所有清洗步骤均实现此接口，支持链式组合。
每个 BaseCleaner 子类只负责一种清洗职责（单一职责原则）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CleanedDocument:
    """
    清洗后的文档单元。

    Attributes:
        content:   清洗后的文本内容
        metadata:  附加元数据（来源文件、页码、文档类型等）
        doc_id:    文档唯一标识（MD5，用于去重）
    """
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    doc_id: str = ""

    def __post_init__(self) -> None:
        if not self.doc_id:
            import hashlib
            self.doc_id = hashlib.md5(
                self.content.encode("utf-8"), usedforsecurity=False
            ).hexdigest()


class BaseCleaner(ABC):
    """
    清洗器抽象基类。

    每个子类实现 clean_one() 处理单条文本；
    基类提供 clean() 批量处理并自动过滤空结果。

    Example:
        >>> class MyStripper(BaseCleaner):
        ...     def clean_one(self, text, metadata):
        ...         return text.strip(), metadata
        >>> cleaner = MyStripper()
        >>> results = cleaner.clean(["  hello  ", "  world  "])
    """

    @abstractmethod
    def clean_one(
        self, text: str, metadata: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """
        清洗单条文本。

        Args:
            text:     输入文本
            metadata: 附加元数据（可修改后返回）

        Returns:
            (cleaned_text, metadata) 元组。
            返回空字符串表示该条应被丢弃。
        """
        ...

    def clean(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> list[CleanedDocument]:
        """
        批量清洗文本列表。

        Args:
            texts:     原始文本列表
            metadatas: 可选元数据列表（与 texts 等长）

        Returns:
            CleanedDocument 列表（已过滤空内容）
        """
        metas = metadatas or [{} for _ in texts]
        results: list[CleanedDocument] = []
        for text, meta in zip(texts, metas):
            try:
                cleaned, updated_meta = self.clean_one(text, meta)
                if cleaned.strip():
                    results.append(CleanedDocument(content=cleaned, metadata=updated_meta))
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("清洗失败，已跳过: %s", e)
        return results
