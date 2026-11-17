"""
朋友圈评论生成节点（异步）

根据检索到的话术生成一条温暖自然的评论（15~40字）。
检索结果为空时仍尝试生成，失败时返回 comment=None。
"""
from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any

from agents.moments.state import MomentsState
from agents.moments.prompts import MOMENTS_COMMENT_SYSTEM, MOMENTS_COMMENT_USER

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _build_llm():
    """创建评论生成 LLM（适当温度保证多样性）。"""
    from langchain_openai import ChatOpenAI
    from agent_framework.config.settings import get_settings
    s = get_settings()
    return ChatOpenAI(
        model=s.generator_model,
        api_key=s.effective_api_key,
        base_url=s.openai_base_url,
        temperature=0.3,
        max_tokens=128,
    )


async def moments_generate_node(state: MomentsState) -> dict[str, Any]:
    """
    评论生成节点（异步）。

    输入:  moment_content, retrieved_chunks
    输出:  comment, debug_info
    """
    t0             = time.time()
    moment_content = state.get("moment_content", "")
    chunks         = state.get("retrieved_chunks") or []

    # 将话术拼接为 context（最多 3 条）
    context = "\n".join(c["content"] for c in chunks[:3]) if chunks else "（暂无参考话术，请自由发挥）"

    comment = None
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        llm  = _build_llm()
        resp = await llm.ainvoke([
            SystemMessage(content=MOMENTS_COMMENT_SYSTEM.format(context=context)),
            HumanMessage(content=MOMENTS_COMMENT_USER.format(moment_content=moment_content)),
        ])
        comment = resp.content.strip()
    except Exception as e:
        logger.warning("[moments/generate] 生成失败，comment=None: %s", e)

    debug_info = dict(state.get("debug_info") or {})
    debug_info["generate_ms"] = round((time.time() - t0) * 1000)

    logger.info("[moments/generate] comment=%r", comment[:30] if comment else None)
    return {
        "comment":    comment,
        "debug_info": debug_info,
    }
