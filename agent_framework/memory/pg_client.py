"""
PostgreSQL 客户端模块

提供异步/同步连接池，封装常用 DDL 和 DML 操作。
当前为「接口预留」实现：连接池和表结构已定义，
业务 SQL（BM25 全文检索等）由上层检索器调用，
后续对接真实数据库时只需配置 .env 中的 PG_* 变量即可。

依赖:
    pip install asyncpg psycopg2-binary
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncGenerator, Generator, Optional

logger = logging.getLogger(__name__)

# ── DDL：知识库文档表 ────────────────────────────────────────────────────────
# 后续在 PostgreSQL 中执行此 SQL 完成建表
CREATE_DOCUMENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id          BIGSERIAL PRIMARY KEY,
    collection  VARCHAR(256) NOT NULL DEFAULT 'default',
    content     TEXT         NOT NULL,
    metadata    JSONB        NOT NULL DEFAULT '{}',
    tokens      TEXT[],                          -- jieba 分词结果，用于 BM25
    tsvector    TSVECTOR GENERATED ALWAYS AS     -- PostgreSQL 全文索引向量
                    (to_tsvector('simple', content)) STORED,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection);
CREATE INDEX IF NOT EXISTS idx_documents_tsvector   ON documents USING GIN(tsvector);
CREATE INDEX IF NOT EXISTS idx_documents_tokens     ON documents USING GIN(tokens);
"""



# ── 模块级连接池单例 ─────────────────────────────────────────────────────────
_pg_client_instance = None

def get_pg_client() -> "PostgresClient":
    """获取全局 PostgresClient 单例，避免重复创建连接池。"""
    global _pg_client_instance
    if _pg_client_instance is None:
        _pg_client_instance = PostgresClient()
    return _pg_client_instance

class PostgresClient:
    """
    PostgreSQL 连接池客户端（同步 psycopg2 实现）。

    后续对接数据库步骤:
        1. 在 .env 中填写 PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DATABASE
        2. 在数据库中执行 CREATE_DOCUMENTS_TABLE_SQL 建表
        3. 实例化 PostgresClient() 即可使用

    Example:
        >>> client = PostgresClient()
        >>> with client.cursor() as cur:
        ...     cur.execute("SELECT 1")
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        pool_min: int = 1,
        pool_max: int = 10,
    ) -> None:
        from agent_framework.config.settings import get_settings
        s = get_settings()
        self._host = host or s.pg_host
        self._port = port or s.pg_port
        self._user = user or s.pg_user
        self._password = password or s.pg_password
        self._database = database or s.pg_database
        self._pool_min = pool_min or s.pg_pool_min
        self._pool_max = pool_max or s.pg_pool_max
        self._pool = None  # 懒加载

    def _get_pool(self):
        """懒加载 psycopg2 连接池。"""
        if self._pool is None:
            try:
                from psycopg2 import pool as pg_pool
                self._pool = pg_pool.ThreadedConnectionPool(
                    minconn=self._pool_min,
                    maxconn=self._pool_max,
                    host=self._host,
                    port=self._port,
                    user=self._user,
                    password=self._password,
                    dbname=self._database,
                )
                logger.info(
                    "PostgreSQL 连接池已建立: %s:%s/%s",
                    self._host, self._port, self._database,
                )
            except ImportError:
                raise ImportError("请安装: pip install psycopg2-binary")
            except Exception as e:
                logger.error("PostgreSQL 连接失败: %s", e)
                raise
        return self._pool

    @contextmanager
    def cursor(self, autocommit: bool = False) -> Generator:
        """
        获取数据库游标的上下文管理器。
        退出时自动提交/回滚并归还连接。

        Example:
            with client.cursor() as cur:
                cur.execute("SELECT id, content FROM documents WHERE collection=%s", (col,))
                rows = cur.fetchall()
        """
        pool = self._get_pool()
        conn = pool.getconn()
        try:
            conn.autocommit = autocommit
            with conn.cursor() as cur:
                yield cur
            if not autocommit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)

    def execute_ddl(self, sql: str) -> None:
        """执行 DDL 语句（建表、建索引等）。"""
        with self.cursor() as cur:
            cur.execute(sql)
        logger.info("DDL 执行完成")

    def init_schema(self) -> None:
        """初始化知识库表结构（幂等）。"""
        self.execute_ddl(CREATE_DOCUMENTS_TABLE_SQL)
        logger.info("documents 表已初始化")

    def close(self) -> None:
        """关闭连接池。"""
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None
            logger.info("PostgreSQL 连接池已关闭")

    def __repr__(self) -> str:
        return f"PostgresClient({self._host}:{self._port}/{self._database})"
