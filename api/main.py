"""
FastAPI 应用入口

启动方式:
    uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

API 文档:
    http://localhost:8000/docs      Swagger UI
    http://localhost:8000/redoc     ReDoc
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routers import health, ingest, cs, moments
from api.routers import admin
from api.middleware import RateLimitMiddleware

logger = logging.getLogger(__name__)


async def _milvus_keepalive():
    """每 5 分钟对所有 collection 做一次轻量检索，防止 Milvus 自动卸载索引。
    检索失败时主动重置连接，触发下次请求时自动重连。"""
    from agents.customer_service.nodes.rag import _get_retriever, _DEFAULT_COLLECTIONS
    from agent_framework.memory.vector_store import MilvusRetriever
    while True:
        await asyncio.sleep(300)  # 5 分钟
        for col in _DEFAULT_COLLECTIONS:
            try:
                _get_retriever(col).retrieve("keepalive", k=1)
                logger.debug("[keepalive] Milvus collection=%s 索引保活成功", col)
            except Exception as e:
                logger.warning("[keepalive] Milvus collection=%s 保活失败，重置连接: %s", col, e)
                try:
                    retriever = _get_retriever(col)
                    # 重置向量检索器内部连接，触发下次请求时重连
                    if hasattr(retriever, '_vector_retriever') and isinstance(retriever._vector_retriever, MilvusRetriever):
                        retriever._vector_retriever._store = None
                except Exception as reset_e:
                    logger.warning("[keepalive] 重置连接失败: %s", reset_e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时预热智能体，关闭时清理资源。"""
    logger.info("正在启动 AI 客服服务...")
    # ── 预热单例 ───────────────────────────────────────────
    from api.dependencies import _get_pipeline, _get_staging, _get_task_runner, _get_cs_agent, _get_moments_agent
    try:
        _get_pipeline()
        _get_staging()
        _get_task_runner()
        await _get_cs_agent()
        _get_moments_agent()

        # RAG 预热：提前初始化检索器并做一次轻量检索，降低首请求冷启动延迟
        try:
            from agents.customer_service.nodes.rag import _get_retriever
            retriever = _get_retriever("products")
            retriever.retrieve("预热", k=1)
            logger.info("RAG 预热完成")
        except Exception as e:
            logger.warning("RAG 预热失败（不影响启动）: %s", e)

        logger.info("服务预热完成")
    except Exception as e:
        logger.warning("预热失败（不影响启动）: %s", e)

    # 初始化商品价格缓存（定时刷新）
    try:
        from agents.customer_service.product_cache import init as init_product_cache
        await init_product_cache()
        logger.info("商品价格缓存初始化完成")
    except Exception as e:
        logger.warning("商品价格缓存初始化失败（不影响启动）: %s", e)

    # 启动 Milvus 保活心跳
    keepalive_task = asyncio.create_task(_milvus_keepalive())
    yield
    keepalive_task.cancel()
    # 停止商品缓存刷新
    try:
        from agents.customer_service.product_cache import stop as stop_product_cache
        stop_product_cache()
    except Exception:
        pass
    # 释放 checkpointer 连接池
    try:
        from api.dependencies import _get_cs_agent
        agent = await _get_cs_agent()
        checkpointer = getattr(agent._graph, "checkpointer", None)
        if checkpointer and hasattr(checkpointer, "_pool"):
            await checkpointer._pool.close()
            logger.info("checkpointer 连接池已释放")
    except Exception as e:
        logger.warning("关闭 checkpointer 连接池失败（不影响退出）: %s", e)
    logger.info("服务已关闭")


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例。"""
    from agent_framework.config.settings import get_settings
    s = get_settings()

    app = FastAPI(
        title="AI 客服智能体 API",
        description="""基于 LangGraph 的电商客服智能体服务

## 接口分类

- **对话** `/chat` — 普通对话和流式对话（SSE）
- **入库** `/ingest` — 文本入库、文件上传、暂存区管理
- **系统** `/health` `/ping` — 健康检查

## 快速开始

1. 发送消息：`POST /chat`
2. 上传知识文档：`POST /ingest/upload`
3. 触发入库：`POST /ingest/staging/run`
        """,
        version="0.1.0",
        docs_url="/docs" if s.is_development else None,
        redoc_url="/redoc" if s.is_development else None,
        openapi_url="/openapi.json" if s.is_development else None,
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 限流 + 鉴权中间件 ──────────────────────────────────
    app.add_middleware(RateLimitMiddleware)

    # ── 注册路由 ───────────────────────────────────────────
    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(cs.router)
    app.include_router(moments.router)
    app.include_router(admin.router)

    # ── 全局异常处理 ───────────────────────────────────────
    from fastapi.exceptions import RequestValidationError
    from fastapi import HTTPException as FastAPIHTTPException

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request, exc):
        body = None
        try:
            body = await request.body()
            body = body.decode("utf-8", errors="replace")
        except Exception:
            pass
        logger.warning("422 校验失败 path=%s errors=%s body_preview=%.200s",
                       request.url.path, exc.errors(), body or "")
        # 提取第一条错误，生成友好提示
        errors = exc.errors()
        if errors:
            e = errors[0]
            field = ".".join(str(x) for x in e.get("loc", [])[1:])  # 去掉 'body' 前缀
            msg   = e.get("msg", "参数错误")
            detail = f"字段 '{field}' 校验失败：{msg}" if field else msg
        else:
            detail = "请求参数格式错误"
        return JSONResponse(
            status_code=422,
            content={"detail": detail},
        )

    @app.exception_handler(FastAPIHTTPException)
    async def http_exception_handler(request, exc):
        # 4xx 原样返回，5xx 隐藏内部细节只记日志
        if exc.status_code >= 500:
            logger.error("5xx path=%s status=%d detail=%s", request.url.path, exc.status_code, exc.detail)
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": "服务内部错误，请稍后重试"},
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request, exc):
        logger.error("未处理异常: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "服务内部错误"},
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    from agent_framework.config.settings import get_settings
    s = get_settings()
    uvicorn.run(
        "api.main:app",
        host=s.api_host,
        port=s.api_port,
        reload=s.is_development,
        log_level=s.log_level.lower(),
    )
