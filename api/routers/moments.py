"""
朋友圈互动 API 路由

POST /moments — 接收朋友圈内容，返回点赞决策和评论
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from api.dependencies import _get_moments_agent

router = APIRouter(prefix="/moments", tags=["朋友圈互动"])


class MomentsRequest(BaseModel):
    content:    str = Field(..., description="朋友圈文字内容", min_length=1, max_length=1000)
    session_id: str = Field(default="default", description="会话 ID，用于日志追踪")


class MomentsResponse(BaseModel):
    like:       bool
    comment:    Optional[str] = None   # 仅 like=True 时有值
    session_id: str


@router.post(
    "",
    response_model=MomentsResponse,
    summary="朋友圈点赞与评论",
    description="分析朋友圈情绪：积极时点赞并生成评论，消极时不做任何操作。",
)
async def moments_interact(request: MomentsRequest) -> MomentsResponse:
    agent  = _get_moments_agent()
    result = await agent.aprocess(
        moment_content=request.content,
        session_id=request.session_id,
    )
    return MomentsResponse(
        like=result["like"],
        comment=result.get("comment"),
        session_id=request.session_id,
    )
