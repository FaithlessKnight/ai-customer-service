"""
依赖注入模块

使用 FastAPI Depends 机制管理 Pipeline 和客服智能体的单例实例。
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

logger = logging.getLogger(__name__)


# ── Pipeline 单例 ──────────────────────────────────────────────
@lru_cache(maxsize=1)
def _get_pipeline():
    """创建并缓存 IngestPipeline 单例（混合检索模式）。"""
    from agent_framework.pipeline.ingest_pipeline import IngestPipeline
    from agent_framework.memory.hybrid_retriever import HybridRetriever
    from agent_framework.memory.vector_store import MilvusRetriever
    from agent_framework.memory.bm25_retriever import BM25Retriever
    retriever = HybridRetriever(
        vector_retriever=MilvusRetriever(collection_name="products"),
        bm25_retriever=BM25Retriever(source="postgresql", collection="products"),
    )
    pipeline = IngestPipeline(retriever=retriever)
    logger.info("IngestPipeline 初始化完成（混合检索模式）")
    return pipeline


# ── StagingArea 单例 ───────────────────────────────────────────
@lru_cache(maxsize=1)
def _get_staging():
    """创建并缓存 StagingArea 单例。路径从 settings.staging_root 读取。"""
    from agent_framework.pipeline.staging import StagingArea
    from agent_framework.config.settings import get_settings
    s = get_settings()
    return StagingArea(staging_root=s.staging_root)


@lru_cache(maxsize=1)
def _get_task_runner():
    """创建并缓存 TaskRunner 单例。"""
    from agent_framework.pipeline.task_runner import TaskRunner
    return TaskRunner(
        staging=_get_staging(),
        pipeline=_get_pipeline(),
    )


# ── 客服智能体异步单例 ─────────────────────────────────────────
_cs_agent = None
_cs_agent_lock = asyncio.Lock()


async def _get_cs_agent():
    """创建并缓存电商客服智能体单例（异步，含 AsyncPostgresSaver）。"""
    global _cs_agent
    if _cs_agent is not None:
        return _cs_agent
    async with _cs_agent_lock:
        if _cs_agent is None:
            from agents.customer_service.agent import CustomerServiceAgent
            _cs_agent = await CustomerServiceAgent.create()
    return _cs_agent


# ── 朋友圈智能体单例 ────────────────────────────────────────────
@lru_cache(maxsize=1)
def _get_moments_agent():
    """创建并缓存朋友圈互动智能体单例。"""
    from agents.moments.agent import MomentsAgent
    agent = MomentsAgent()
    logger.info("MomentsAgent 初始化完成")
    return agent


# ── FastAPI Depends 函数 ───────────────────────────────────────
def get_pipeline():
    return _get_pipeline()


def get_staging():
    return _get_staging()


def get_task_runner():
    return _get_task_runner()


async def get_cs_agent():
    return await _get_cs_agent()


def get_moments_agent():
    return _get_moments_agent()


# ── 类型别名 ───────────────────────────────────────────────────
PipelineDep     = Annotated[object, Depends(get_pipeline)]
StagingDep      = Annotated[object, Depends(get_staging)]
TaskRunnerDep   = Annotated[object, Depends(get_task_runner)]
CSAgentDep      = Annotated[object, Depends(get_cs_agent)]
MomentsAgentDep = Annotated[object, Depends(get_moments_agent)]
