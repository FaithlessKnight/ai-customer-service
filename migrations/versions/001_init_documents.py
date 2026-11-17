"""初始化知识库文档表

将现有 pg_client.py 中的 CREATE_DOCUMENTS_TABLE_SQL 纳入版本管理。

Revision ID: 001
Revises:     (无，初始版本)
Create Date: 2026-03-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    创建 documents 表及索引。

    字段说明：
      id          — 自增主键
      collection  — 知识库分区（可多租户隔离）
      content     — 文档原始文本
      metadata    — 业务元数据（doc_type / category / source 等）
      tokens      — jieba 分词结果，用于 BM25 检索
      tsvector    — PostgreSQL 全文索引向量（自动生成列）
      created_at  — 入库时间
    """
    # ── 建表 ───────────────────────────────────────────────────
    op.create_table(
        "documents",
        sa.Column("id",         sa.BigInteger,    primary_key=True, autoincrement=True),
        sa.Column("collection", sa.String(256),   nullable=False,   server_default="default"),
        sa.Column("content",    sa.Text,          nullable=False),
        sa.Column("metadata",   postgresql.JSONB, nullable=False,   server_default="{}"),
        sa.Column("tokens",     postgresql.ARRAY(sa.Text)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # ── tsvector 计算列（PostgreSQL 12+ 支持 GENERATED ALWAYS AS STORED）─
    op.execute("""
        ALTER TABLE documents
        ADD COLUMN tsvector TSVECTOR
        GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
    """)

    # ── 索引 ───────────────────────────────────────────────────
    op.create_index(
        "idx_documents_collection",
        "documents",
        ["collection"],
    )
    op.create_index(
        "idx_documents_tsvector",
        "documents",
        ["tsvector"],
        postgresql_using="gin",
    )
    op.create_index(
        "idx_documents_tokens",
        "documents",
        ["tokens"],
        postgresql_using="gin",
    )
    op.create_index(
        "idx_documents_created_at",
        "documents",
        ["created_at"],
    )


def downgrade() -> None:
    """删除 documents 表及所有索引（完全回滚）。"""
    op.drop_index("idx_documents_created_at", table_name="documents")
    op.drop_index("idx_documents_tokens",     table_name="documents")
    op.drop_index("idx_documents_tsvector",   table_name="documents")
    op.drop_index("idx_documents_collection", table_name="documents")
    op.drop_table("documents")
