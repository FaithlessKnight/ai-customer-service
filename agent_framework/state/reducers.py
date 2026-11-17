"""
自定义 Reducer 函数

Reducer 决定了当多个节点更新同一字段时如何合并状态。
LangGraph 默认行为是覆盖（last-write-wins），
此模块提供常用的自定义合并策略。
"""

from typing import Any


def add_messages(left: list, right: list) -> list:
    """
    消息追加 reducer（LangGraph 内置，此处作为文档参考）。
    将新消息追加到历史列表，而非覆盖。
    """
    return left + right


def merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """
    字典深度合并 reducer。
    右侧值覆盖左侧同名键，新键直接添加。

    Example:
        >>> merge_dicts({"a": 1, "b": 2}, {"b": 99, "c": 3})
        {'a': 1, 'b': 99, 'c': 3}
    """
    return {**left, **right}


def append_list(left: list, right: list) -> list:
    """
    列表追加 reducer（去重版本）。
    将 right 中不存在于 left 的元素追加进去。
    """
    seen = set(map(str, left))
    return left + [item for item in right if str(item) not in seen]


def keep_last(left: Any, right: Any) -> Any:
    """
    保留最新值 reducer（默认行为，显式声明语义）。
    """
    return right if right is not None else left


def increment(left: int, right: int) -> int:
    """
    数值累加 reducer，适合计数器场景（如 iteration）。

    Example:
        >>> increment(3, 1)
        4
    """
    return left + right
