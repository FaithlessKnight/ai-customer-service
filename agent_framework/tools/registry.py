"""
工具注册表

集中管理所有可用工具，支持按名称或标签检索。
智能体可从注册表中按需选取工具组合。
"""

from functools import lru_cache
from typing import Optional

from langchain_core.tools import BaseTool


class ToolRegistry:
    """
    工具注册表：统一存储和检索 LangChain 工具。

    Example:
        >>> registry = ToolRegistry()
        >>> registry.register(my_tool, tags=["search"])
        >>> search_tools = registry.get_by_tag("search")
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._tags: dict[str, list[str]] = {}  # tool_name -> [tag1, tag2]

    def register(
        self,
        tool: BaseTool,
        tags: Optional[list[str]] = None,
        override: bool = False,
    ) -> "ToolRegistry":
        """
        注册一个工具。

        Args:
            tool:     LangChain BaseTool 实例
            tags:     工具标签列表，用于分组检索
            override: 是否允许覆盖同名工具（默认不允许）

        Returns:
            self（支持链式调用）
        """
        if tool.name in self._tools and not override:
            raise ValueError(
                f"工具 '{tool.name}' 已注册。使用 override=True 强制覆盖。"
            )
        self._tools[tool.name] = tool
        self._tags[tool.name] = tags or []
        return self

    def register_many(
        self,
        tools: list[BaseTool],
        tags: Optional[list[str]] = None,
    ) -> "ToolRegistry":
        """批量注册工具。"""
        for tool in tools:
            self.register(tool, tags=tags)
        return self

    def get(self, name: str) -> BaseTool:
        """按名称获取工具，不存在时抛出 KeyError。"""
        if name not in self._tools:
            raise KeyError(f"工具 '{name}' 未注册。已注册工具: {self.list_names()}")
        return self._tools[name]

    def get_by_tag(self, tag: str) -> list[BaseTool]:
        """按标签获取工具列表。"""
        return [
            self._tools[name]
            for name, tags in self._tags.items()
            if tag in tags
        ]

    def get_many(self, names: list[str]) -> list[BaseTool]:
        """按名称列表批量获取工具。"""
        return [self.get(name) for name in names]

    def list_names(self) -> list[str]:
        """列出所有已注册工具名称。"""
        return list(self._tools.keys())

    def all(self) -> list[BaseTool]:
        """获取所有已注册工具。"""
        return list(self._tools.values())

    def __repr__(self) -> str:
        return f"ToolRegistry(tools={self.list_names()})"


@lru_cache(maxsize=1)
def get_tool_registry() -> ToolRegistry:
    """
    获取全局工具注册表单例。
    首次调用时自动注册内置工具。
    """
    from agent_framework.tools.search import web_search_tool, duckduckgo_search_tool
    from agent_framework.tools.calculator import calculator_tool
    from agent_framework.tools.datetime_tool import datetime_tool
    from agent_framework.tools.file_tool import read_file_tool, write_file_tool

    registry = ToolRegistry()
    registry.register_many(
        [web_search_tool, duckduckgo_search_tool],
        tags=["search", "internet"],
    )
    registry.register(calculator_tool, tags=["math", "utility"])
    registry.register(datetime_tool, tags=["utility", "time"])
    registry.register_many(
        [read_file_tool, write_file_tool],
        tags=["file", "io"],
    )
    return registry
