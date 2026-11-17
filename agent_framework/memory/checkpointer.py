"""
检查点模块

LangGraph 的 Checkpointer 实现持久化状态，
支持多轮对话恢复和断点续跑。
"""

from typing import Literal, Optional


def get_checkpointer(backend: Literal["memory", "sqlite", "redis"] = "memory", **kwargs):
    """
    工厂函数：创建 LangGraph Checkpointer 实例。

    Args:
        backend: 存储后端
            - 'memory':  内存存储（默认，进程退出后丢失）
            - 'sqlite':  SQLite 文件存储（持久化，单机）
            - 'redis':   Redis 存储（持久化，可分布式）
        **kwargs: 传递给后端的额外参数

    Returns:
        BaseCheckpointSaver 实例

    Example:
        >>> checkpointer = get_checkpointer("sqlite", db_path="./data/checkpoints.db")
        >>> graph = builder.compile(checkpointer=checkpointer)
        >>> # 使用 thread_id 区分会话
        >>> config = {"configurable": {"thread_id": "user-123"}}
        >>> result = graph.invoke(state, config=config)
    """
    if backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()

    elif backend == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
            db_path = kwargs.get("db_path", "./data/checkpoints.db")
            import sqlite3
            conn = sqlite3.connect(db_path, check_same_thread=False)
            return SqliteSaver(conn)
        except ImportError:
            raise ImportError(
                "SQLite checkpointer 需要安装: pip install langgraph-checkpoint-sqlite"
            )

    elif backend == "redis":
        try:
            from langgraph.checkpoint.redis import RedisSaver
            from agent_framework.config.settings import get_settings
            redis_url = kwargs.get("redis_url") or get_settings().redis_url
            return RedisSaver.from_conn_string(redis_url)
        except ImportError:
            raise ImportError(
                "Redis checkpointer 需要安装: pip install langgraph-checkpoint-redis"
            )

    else:
        raise ValueError(f"不支持的存储后端: {backend}。支持: memory, sqlite, redis")
