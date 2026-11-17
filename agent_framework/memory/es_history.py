"""
Elasticsearch 会话历史加载模块

从外部系统的 ES（ipad_chat_message 索引）中拉取指定用户的会话历史，
转换为 LangChain messages 格式供智能体使用。

字段映射：
  - session_id → sender（用户ID，如 "7881302044079929"）
  - 角色区分  → is_ai（0=用户，1=AI）
  - 消息内容  → content
  - 时间排序  → send_time（Unix 秒时间戳）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger(__name__)


ES_INDEX        = "ipad_chat_message"
FIELD_SESSION   = "sender"
FIELD_ROLE      = "is_ai"
FIELD_CONTENT   = "content"
FIELD_TIMESTAMP = "send_time"
ROLE_USER       = 0
ROLE_ASSISTANT  = 1


def _get_es_client():
    """创建 ES 客户端（从 settings 读取连接配置）。"""
    from agent_framework.config.settings import get_settings
    try:
        from elasticsearch import Elasticsearch
    except ImportError:
        raise ImportError("请安装: pip install elasticsearch")
    s = get_settings()
    return Elasticsearch(hosts=[{"host": s.es_host, "port": s.es_port, "scheme": s.es_scheme}])


async def load_session_history(
    session_id: str,
    max_messages: int = 100,
    max_age_days: int = 90,
) -> list[BaseMessage]:
    """
    从 ES 加载指定用户（sender）的会话历史。

    Args:
        session_id:   用户ID，对应 ES 的 sender 字段
        max_messages: 最多加载的消息条数
        max_age_days: 只加载最近 N 天内的消息，防止历史无限增长（默认90天）

    Returns:
        按 send_time 升序排列的 LangChain messages 列表，
        ES 不可用时返回空列表（不阻断流程）
    """
    if not ES_INDEX:
        logger.debug("[es_history] ES 未配置，跳过历史加载")
        return []

    try:
        import time as _time
        client = _get_es_client()
        cutoff_ts = int(_time.time()) - max_age_days * 86400

        def _query():
            resp = client.search(
                index=ES_INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": [{"term": {FIELD_SESSION: session_id}}],
                            "filter": [{"range": {FIELD_TIMESTAMP: {"gte": cutoff_ts}}}],
                        }
                    },
                    "sort": [{FIELD_TIMESTAMP: {"order": "asc"}}],
                    "size": max_messages,
                },
            )
            return resp["hits"]["hits"]

        hits = await asyncio.to_thread(_query)

        messages: list[BaseMessage] = []
        for hit in hits:
            src     = hit["_source"]
            role    = src.get(FIELD_ROLE)
            content = src.get(FIELD_CONTENT, "").strip()
            if not content:
                continue
            if role == ROLE_USER:
                messages.append(HumanMessage(content=content))
            elif role == ROLE_ASSISTANT:
                messages.append(AIMessage(content=content))

        logger.info("[es_history] sender=%s 加载 %d 条历史消息", session_id, len(messages))
        return messages

    except Exception as e:
        logger.warning("[es_history] 加载历史失败，降级为空历史: %s", e)
        return []
