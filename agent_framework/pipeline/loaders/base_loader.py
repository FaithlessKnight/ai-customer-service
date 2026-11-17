"""
加载器抽象基类

定义统一的文档加载接口，屏蔽不同格式的读取细节。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LoadedDocument:
    """
    加载后的原始文档。

    Attributes:
        content:   原始文本内容（未清洗）
        metadata:  来源信息（文件名、路径、页码、格式等）
        source:    来源路径或标识符
    """
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = ""


class BaseLoader(ABC):
    """
    文档加载器抽象基类。

    子类实现 load() 方法，返回 LoadedDocument 列表。
    一个文件可能产生多个 LoadedDocument（如 PDF 按页拆分）。
    """

    @abstractmethod
    def load(self, source: str | Path) -> list[LoadedDocument]:
        """
        加载文档。

        Args:
            source: 文件路径或 URL

        Returns:
            LoadedDocument 列表
        """
        ...

    def load_many(self, sources: list[str | Path]) -> list[LoadedDocument]:
        """批量加载多个文档。"""
        results: list[LoadedDocument] = []
        for src in sources:
            try:
                results.extend(self.load(src))
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("加载失败 [%s]: %s", src, e)
        return results
