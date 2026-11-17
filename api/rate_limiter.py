"""
分布式限流器（Redis + 进程内降级）

多实例部署下通过 Redis 实现全局一致的限流计数。
Redis 不可用时自动降级为进程内滑动窗口，保证服务不中断。

限流算法：固定窗口计数器（Redis INCR + EXPIRE）
  - key 格式: rl:{type}:{identifier}:{minute_bucket}
  - 每个自然分钟一个 bucket，INCR 原子递增，首次写入时设 EXPIRE 120s
  - 单次 Redis 调用 ~1ms，对请求延迟影响极小
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# ── 进程内降级计数器 ──────────────────────────────────────────
_local_counters: dict[str, deque] = defaultdict(deque)


def _local_is_allowed(key: str, limit: int, window: int = 60) -> bool:
    """进程内滑动窗口（降级用，单进程内有效）。"""
    now = time.monotonic()
    q = _local_counters[key]
    cutoff = now - window
    while q and q[0] <= cutoff:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def _local_cleanup() -> None:
    """清理过期的本地计数器 key。"""
    now = time.monotonic()
    stale = [k for k, q in _local_counters.items() if not q or q[-1] <= now - 120]
    for k in stale:
        del _local_counters[k]


# ── Redis 分布式限流器 ────────────────────────────────────────
class RedisRateLimiter:
    """
    基于 Redis 的分布式限流器。

    特性：
    - 固定窗口计数（每自然分钟一个 bucket）
    - 原子 INCR + EXPIRE，多实例共享计数
    - Redis 不可用时自动降级为进程内计数
    - 支持全局、IP、session 三种维度

    用法：
        limiter = RedisRateLimiter(redis_url)
        await limiter.initialize()
        allowed = await limiter.is_allowed("ip", "10.0.1.5", limit=30)
    """

    def __init__(self, redis_url: str = "") -> None:
        self._redis_url = redis_url
        self._client: aioredis.Redis | None = None
        self._available = False
        self._cleanup_counter = 0

    async def initialize(self) -> None:
        """初始化 Redis 连接。启动时调用一次。"""
        if not self._redis_url:
            logger.info("[rate_limiter] Redis URL 未配置，使用进程内限流")
            return
        try:
            self._client = aioredis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=2,
                retry_on_timeout=True,
                max_connections=20,
            )
            await self._client.ping()
            self._available = True
            logger.info("[rate_limiter] Redis 限流器初始化成功")
        except Exception as e:
            logger.warning("[rate_limiter] Redis 连接失败，降级为进程内限流: %s", e)
            self._available = False

    async def is_allowed(self, rate_type: str, identifier: str, limit: int) -> bool:
        """
        检查是否允许本次请求。

        Args:
            rate_type: 限流维度（"global" / "ip" / "session"）
            identifier: 标识符（"__global__" / IP 地址 / session_id）
            limit: 每分钟上限

        Returns:
            True 允许，False 触发限流
        """
        # Redis 可用时走分布式计数
        if self._available and self._client:
            try:
                return await self._redis_check(rate_type, identifier, limit)
            except Exception as e:
                logger.warning("[rate_limiter] Redis 调用失败，降级本地: %s", e)
                self._available = False

        # 降级：进程内计数
        local_key = f"rl:{rate_type}:{identifier}"
        self._cleanup_counter += 1
        if self._cleanup_counter >= 500:
            _local_cleanup()
            self._cleanup_counter = 0
        return _local_is_allowed(local_key, limit)

    async def _redis_check(self, rate_type: str, identifier: str, limit: int) -> bool:
        """Redis 固定窗口计数。"""
        # 当前分钟 bucket：以 Unix 时间的分钟数为 key
        minute_bucket = int(time.time()) // 60
        key = f"rl:{rate_type}:{identifier}:{minute_bucket}"

        count = await self._client.incr(key)
        if count == 1:
            # 首次写入，设置过期时间（120s，覆盖当前分钟 + 下一分钟窗口）
            await self._client.expire(key, 120)

        return count <= limit

    async def close(self) -> None:
        """关闭 Redis 连接。"""
        if self._client:
            await self._client.aclose()
            self._client = None
            self._available = False


# ── 模块级单例 ────────────────────────────────────────────────
_limiter: RedisRateLimiter | None = None


async def get_rate_limiter() -> RedisRateLimiter:
    """获取全局限流器单例（首次调用时初始化）。"""
    global _limiter
    if _limiter is None:
        from agent_framework.config.settings import get_settings
        s = get_settings()
        _limiter = RedisRateLimiter(redis_url=s.redis_url)
        await _limiter.initialize()
    return _limiter
