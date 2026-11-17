"""Alembic 迁移环境配置

从项目 .env 动态读取数据库连接信息，无需在 alembic.ini 里硬编码密码。
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 把项目根目录加入 sys.path，确保能 import agent_framework
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 读取 alembic.ini 中的日志配置
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── 从项目 settings 动态获取数据库 URL ───────────────────────────
def get_db_url() -> str:
    from agent_framework.config.settings import get_settings
    s = get_settings()
    return (
        f"postgresql+psycopg2://{s.pg_user}:{s.pg_password}"
        f"@{s.pg_host}:{s.pg_port}/{s.pg_database}"
    )


# ── 离线模式（生成 SQL 脚本，不连接数据库）───────────────────────
def run_migrations_offline() -> None:
    """生成迁移 SQL 脚本（不需要数据库连接）。"""
    url = get_db_url()
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ── 在线模式（直接连接数据库执行迁移）──────────────────────────────
def run_migrations_online() -> None:
    """直接连接数据库执行迁移。"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_db_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # 迁移时不需要连接池
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=None,
            # 事务性 DDL（PostgreSQL 支持，出错自动回滚）
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
