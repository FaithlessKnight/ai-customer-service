"""
API 请求 / 响应数据模型

所有接口的输入输出均通过此处的 Pydantic 模型定义，
保证类型安全和自动生成 OpenAPI 文档。
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


# ── 通用 ──────────────────────────────────────────────────────
class OKResponse(BaseModel):
    """通用成功响应。"""
    ok: bool = True
    message: str = "success"


class ErrorResponse(BaseModel):
    """通用错误响应。"""
    ok: bool = False
    error: str
    detail: Optional[str] = None


# ── 对话接口 ──────────────────────────────────────────────────
class ChatRequest(BaseModel):
    """普通对话请求。"""
    message: str = Field(..., description="用户输入的消息", min_length=1, max_length=4096)
    session_id: str = Field(default="default", description="会话 ID，用于多轮对话记忆隔离")
    agent_type: Literal["rag", "react"] = Field(
        default="rag", description="智能体类型：rag（知识库问答）/ react（工具调用）"
    )
    retrieve_k: int = Field(default=5, ge=1, le=20, description="RAG 检索返回的文档数量")


class ChatResponse(BaseModel):
    """普通对话响应。"""
    reply: str = Field(..., description="AI 回复内容")
    session_id: str
    agent_type: str
    sources: list[dict[str, Any]] = Field(
        default_factory=list, description="RAG 检索来源（文档块元数据）"
    )


class StreamChatRequest(BaseModel):
    """流式对话请求（SSE）。"""
    message: str = Field(..., description="用户输入的消息", min_length=1, max_length=4096)
    session_id: str = Field(default="default", description="会话 ID")
    agent_type: Literal["rag", "react"] = Field(default="rag")
    retrieve_k: int = Field(default=5, ge=1, le=20)


# ── 数据入库接口 ──────────────────────────────────────────────
class IngestTextRequest(BaseModel):
    """直接传入文本入库。"""
    texts: list[str] = Field(..., description="文本列表", min_length=1)
    metadatas: Optional[list[dict[str, Any]]] = Field(
        default=None, description="元数据列表，与 texts 等长"
    )
    collection: str = Field(default="default", description="知识库集合名")


class IngestResponse(BaseModel):
    """入库结果响应。"""
    ok: bool = True
    total_chunks: int
    ingested: int
    skipped: int
    errors: list[str] = Field(default_factory=list)
    summary: str


class StagingUploadResponse(BaseModel):
    """文件上传到暂存区的响应。"""
    file_id: str
    filename: str
    status: str
    message: str


class StagingStatusResponse(BaseModel):
    """暂存区状态响应。"""
    pending: int
    processing: int
    done: int
    failed: int
    files: list[dict[str, Any]]


class StagingRunRequest(BaseModel):
    """触发暂存区文件入库的请求。"""
    file_id: Optional[str] = Field(default=None, description="指定处理单个文件 ID，为空则处理全部 pending")
    filter_meta: Optional[dict[str, Any]] = Field(
        default=None, description="按元数据过滤，如 {\"doc_type\": \"sop\"}"
    )


# ── 健康检查 ──────────────────────────────────────────────────
class HealthResponse(BaseModel):
    """健康检查响应。"""
    status: Literal["ok", "degraded", "error"]
    version: str
    environment: str
    components: dict[str, str] = Field(
        default_factory=dict, description="各组件状态"
    )
