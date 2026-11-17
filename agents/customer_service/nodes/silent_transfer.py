"""
售后类静默转人工：不向客户生成话术（reply 为空），由 CRM 侧发话；
仍写入 Human/AI 消息以保持会话链与记忆一致。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from agents.customer_service.state import CustomerServiceState

logger = logging.getLogger(__name__)


async def silent_transfer_node(state: CustomerServiceState) -> dict[str, Any]:
    t0 = time.time()
    user_input = state.get("user_input", "")
    session_id = state.get("session_id", "")
    existing_msgs = list(state.get("messages") or [])
    new_messages = list(existing_msgs)
    new_messages.append(HumanMessage(content=user_input))
    new_messages.append(AIMessage(content=""))
    if len(new_messages) > 40:
        new_messages = new_messages[-40:]

    debug_info = dict(state.get("debug_info") or {})
    debug_info["silent_transfer_ms"] = round((time.time() - t0) * 1000)
    debug_info["reply_parts"] = 0

    logger.info(
        "[silent_transfer] session=%s reason=%s (empty reply, CRM handles messaging)",
        session_id,
        state.get("transfer_reason"),
    )
    return {
        "final_reply": "",
        "reply_parts": [],
        "messages": new_messages,
        "debug_info": debug_info,
    }
