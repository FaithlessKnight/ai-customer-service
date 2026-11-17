"""
限流与鉴权中间件

提供三层防护：
  1. API Key 鉴权（生产环境开启）
  2. 全局每分钟请求上限
  3. 每 IP 每分钟请求上限

实现方式：Redis 分布式限流（支持多实例部署）。
Redis 不可用时自动降级为进程内滑动窗口。
"""
from __future__ import annotations

import logging
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# 不参与限流的路径
_EXEMPT_PATHS = {"/health", "/ping", "/docs", "/redoc", "/openapi.json"}

# 豁免 IP 限流的地址（本机回环 + 内网 Docker，不对外暴露，不存在外部攻击面）
_EXEMPT_IPS = {"127.0.0.1", "::1", "localhost"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    限流 + 鉴权中间件（Redis 分布式版）。

    检查顺序：
    1. 豁免路径 → 直接放行
    2. API Key 鉴权（secret_key 非空时启用）
    3. 全局限流（Redis）
    4. IP 限流（Redis）
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        from agent_framework.config.settings import get_settings
        s = get_settings()
        self._enabled      = s.rate_limit_enabled
        self._ip_limit     = s.rate_limit_ip_per_minute
        self._global_limit = s.rate_limit_global_per_minute
        self._secret_key   = s.api_secret_key
        self._key_header   = s.api_key_header

    def _get_client_ip(self, request: Request) -> str:
        """获取真实客户端 IP（兼容反向代理）。"""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # 1. 豁免路径直接放行
        if path in _EXEMPT_PATHS or not self._enabled:
            return await call_next(request)

        # 2. API Key 鉴权
        if self._secret_key:
            provided_key = request.headers.get(self._key_header, "")
            if provided_key != self._secret_key:
                logger.warning("[限流] 鉴权失败 path=%s ip=%s", path, self._get_client_ip(request))
                return JSONResponse(
                    status_code=401,
                    content={"ok": False, "error": "Unauthorized", "detail": f"请在 {self._key_header} Header 中提供有效的 API Key"},
                )

        client_ip = self._get_client_ip(request)

        # 获取 Redis 分布式限流器
        from api.rate_limiter import get_rate_limiter
        limiter = await get_rate_limiter()

        # 3. 全局限流
        if not await limiter.is_allowed("global", "__global__", self._global_limit):
            logger.warning("[限流] 全局限流触发 path=%s", path)
            return JSONResponse(
                status_code=429,
                content={"ok": False, "error": "Too Many Requests", "detail": "服务繁忙，请稍后再试"},
                headers={"Retry-After": "10"},
            )

        # 4. IP 限流（本机回环地址豁免）
        if client_ip not in _EXEMPT_IPS and not await limiter.is_allowed("ip", client_ip, self._ip_limit):
            logger.warning("[限流] IP 限流触发 ip=%s path=%s", client_ip, path)
            return JSONResponse(
                status_code=429,
                content={"ok": False, "error": "Too Many Requests", "detail": f"请求过于频繁，每分钟最多 {self._ip_limit} 次，请稍后再试"},
                headers={"Retry-After": "60"},
            )

        return await call_next(request)
