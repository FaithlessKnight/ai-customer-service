"""
朋友圈互动智能体 State 定义

字段分为：输入 / emotion_node 写 / rag_node 写 / generate_node 写 / 调试
"""
from __future__ import annotations

from typing import Any, Optional
from typing_extensions import Annotated, TypedDict


def _merge_debug(a: dict, b: dict) -> dict:
    merged = dict(a or {})
    merged.update(b or {})
    return merged


class MomentsState(TypedDict):
    # ── 输入 ──────────────────────────────────────────────
    moment_content: str       # 朋友圈文字内容
    session_id:     str

    # ── emotion_node 写 ───────────────────────────────────
    is_positive: bool         # True=积极点赞+评论, False=消极不动作

    # ── rag_node 写（仅 is_positive=True 时填充）───────────
    retrieved_chunks: list[dict[str, Any]]

    # ── generate_node 写（仅 is_positive=True 时填充）───────
    comment: Optional[str]

    # ── 调试信息 ──────────────────────────────────────────
    debug_info: Annotated[dict[str, Any], _merge_debug]
