"""
朋友圈情绪识别节点（异步）

判断朋友圈内容的情绪：positive（积极）或 negative（消极）。
积极 → is_positive=True，触发后续点赞+评论流程。
消极 → is_positive=False，不做任何操作。
解析失败时保守降级为 False，避免误点赞。
使用 with_structured_output 强制结构化输出，避免模型多余文字导致解析失败。
"""
from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.moments.state import MomentsState
from agents.moments.prompts import MOMENTS_EMOTION_SYSTEM, MOMENTS_EMOTION_USER

logger = logging.getLogger(__name__)


class MomentsSentiment(BaseModel):
    sentiment: Literal["positive", "negative"] = Field(description="情绪倾向")
    reason:    str                              = Field(description="判断理由")


@lru_cache(maxsize=1)
def _build_llm():
    """创建情绪分类 LLM，强制结构化输出。"""
    from langchain_openai import ChatOpenAI
    from agent_framework.config.settings import get_settings
    s   = get_settings()
    llm = ChatOpenAI(
        model=s.generator_model,
        api_key=s.effective_api_key,
        base_url=s.openai_base_url,
        temperature=0.0,
    )
    return llm.with_structured_output(MomentsSentiment)


async def moments_emotion_node(state: MomentsState) -> dict[str, Any]:
    """
    朋友圈情绪识别节点（异步）。

    输入:  moment_content, session_id
    输出:  is_positive, debug_info
    """
    t0             = time.time()
    moment_content = state.get("moment_content", "")
    session_id     = state.get("session_id", "default")

    prompt      = MOMENTS_EMOTION_USER.format(moment_content=moment_content)
    is_positive = False
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        llm    = _build_llm()
        result: MomentsSentiment = await llm.ainvoke([
            SystemMessage(content=MOMENTS_EMOTION_SYSTEM),
            HumanMessage(content=prompt),
        ])
        is_positive = result.sentiment == "positive"
    except Exception as e:
        logger.warning("[moments/emotion] 识别失败，降级为 False: %s", e)

    debug_info = dict(state.get("debug_info") or {})
    debug_info["emotion_ms"] = round((time.time() - t0) * 1000)

    logger.info("[moments/emotion] session=%s is_positive=%s", session_id, is_positive)
    return {
        "is_positive": is_positive,
        "debug_info":  debug_info,
    }
