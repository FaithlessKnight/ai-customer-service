"""
检索器抽象基类

定义统一的检索器接口契约。
Milvus 向量检索、PostgreSQL BM25 检索、混合检索均实现此接口，
保证上层（RAG 节点、Agent）与具体存储实现完全解耦。
后续对接真实数据库时只需替换具体实现类，上层代码无需改动。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


def match_filters(metadata: dict[str, Any], filters: Optional[dict[str, Any]]) -> bool:
    """
    Python 侧 metadata 过滤匹配。

    filters 值可以是：
      - 标量：精确匹配（`meta[key] == value`）
      - list/tuple/set：IN 匹配（`meta[key] in value`）
    filters=None 或 空 → 永远返回 True。
    """
    if not filters:
        return True
    for key, expected in filters.items():
        actual = metadata.get(key)
        if isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        else:
            if actual != expected:
                return False
    return True


def build_milvus_expr(filters: Optional[dict[str, Any]]) -> Optional[str]:
    """
    将 filters 字典转成 Milvus boolean expression。
      {"scene": "trial_not_used"}            → "scene == \"trial_not_used\""
      {"scene": ["trial_not_used", "any"]}   → "scene in [\"trial_not_used\", \"any\"]"
      {"a": 1, "b": [2, 3]}                  → "a == 1 && b in [2, 3]"
    filters 为 None / 空 时返回 None。
    字符串自动加双引号，数字原样输出。
    """
    if not filters:
        return None
    parts = []
    for key, val in filters.items():
        if isinstance(val, (list, tuple, set)):
            items = [_milvus_literal(v) for v in val]
            parts.append(f"{key} in [{', '.join(items)}]")
        else:
            parts.append(f"{key} == {_milvus_literal(val)}")
    return " && ".join(parts)


def _milvus_literal(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    # 字符串：转义双引号
    s = str(v).replace('"', '\\"')
    return f'"{s}"'


@dataclass
class RetrievedChunk:
    """
    检索结果数据类。

    Attributes:
        content:    文本内容
        score:      相关性得分（越高越相关，归一化到 0~1 或原始分数均可）
        metadata:   附加元数据（文档 ID、来源、页码等）
        source:     检索来源标识，如 'milvus' / 'bm25' / 'hybrid'
    """
    content: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "score": self.score,
            "metadata": self.metadata,
            "source": self.source,
        }


class BaseRetriever(ABC):
    """
    检索器抽象基类。

    所有检索器（向量/BM25/混合）均继承此类。
    上层代码只依赖此接口，与具体数据库实现解耦。

    Example:
        # 后续对接真实数据库，只需替换实现类：
        retriever: BaseRetriever = HybridRetriever(...)
        results = retriever.retrieve("怎么退货", k=5)
    """

    @abstractmethod
    def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: Optional[dict[str, Any]] = None,
    ) -> list[RetrievedChunk]:
        """
        检索与 query 最相关的 k 条文档。

        Args:
            query:   查询文本（自然语言）
            k:       返回结果数量
            filters: metadata 过滤条件，形如
                     {"scene": "trial_not_used"}                    # 精确匹配
                     {"scene": ["trial_not_used", "any"]}           # IN 匹配
                     为 None 时不过滤（向后兼容）

        Returns:
            RetrievedChunk 列表，按相关性降序排列
        """
        ...

    @abstractmethod
    def add_documents(
        self,
        texts: list[str],
        metadatas: Optional[list[dict[str, Any]]] = None,
    ) -> list[str]:
        """
        向存储中插入文档。

        Args:
            texts:     文本列表
            metadatas: 可选元数据列表（与 texts 等长）

        Returns:
            插入文档的 ID 列表
        """
        ...

    def format_context(self, query: str, k: int = 5) -> str:
        """
        检索并格式化为可直接注入 Prompt 的上下文字符串。
        默认实现基于 retrieve()，子类可覆写。

        Args:
            query: 查询文本
            k:     检索数量

        Returns:
            格式化上下文字符串
        """
        chunks = self.retrieve(query, k=k)
        if not chunks:
            return "[未找到相关知识库内容]"
        parts = [
            f"[{i + 1}] {chunk.content}"
            + (f"  (来源: {chunk.metadata.get('source', chunk.source)})" if chunk.metadata else "")
            for i, chunk in enumerate(chunks)
        ]
        return "\n\n".join(parts)
