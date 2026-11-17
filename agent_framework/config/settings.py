"""
全局配置管理模块

使用 pydantic-settings 从环境变量或 .env 文件加载配置。
支持多环境 (development / production / testing)。
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置，自动从 .env 文件和环境变量加载。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 运行环境 ──────────────────────────────────────────
    environment: Literal["development", "production", "testing"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    allowed_origins: list[str] = Field(
        default=["*"],
        description="CORS 允许的来源列表，生产环境应设为实际域名，如 https://example.com"
    )

    # ── OpenAI / 通义千问（DashScope 兼容接口）────────────────
    openai_api_key: str = Field(default="", description="API Key（OpenAI 或 DashScope）")
    dashscope_api_key: str = Field(default="", description="DashScope API Key 别名，与 openai_api_key 二选一")
    openai_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="API Base URL（通义千问 DashScope 兼容接口）"
    )
    openai_model: str = Field(default="deepseek-v4-flash", description="默认 LLM 模型")
    openai_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    openai_max_tokens: int = Field(default=1024, gt=0)

    @property
    def effective_api_key(self) -> str:
        """优先使用 dashscope_api_key，其次 openai_api_key。"""
        return self.dashscope_api_key or self.openai_api_key

    # ── Anthropic（可选） ──────────────────────────────────
    anthropic_api_key: str = Field(default="", description="Anthropic API Key")
    anthropic_model: str = Field(default="claude-3-5-sonnet-20241022")

    # ── 工具 ──────────────────────────────────────────────
    tavily_api_key: str = Field(default="", description="Tavily Search API Key")

    # ── LangSmith ─────────────────────────────────────────
    langchain_tracing_v2: bool = False
    langchain_api_key: str = Field(default="")
    langchain_project: str = Field(default="ai-agent-framework")

    # ── Embedding 模型 ────────────────────────────────────
    embedding_model: str = Field(default="bge-m3")
    embedding_dim: int = Field(default=1024, description="向量维度，需与 embedding 模型匹配")
    embedding_provider: str = Field(default="ollama", description="embedding 提供商：openai 或 ollama")

    # ── Ollama（本地模型） ────────────────────────────────
    ollama_base_url: str = Field(default="http://localhost:11434", description="Ollama 服务地址")
    ollama_embedding_model: str = Field(default="gemma-300m", description="Ollama Embedding 模型名")
    ollama_embed_batch_size: int = Field(
        default=2,
        ge=1,
        le=32,
        description="Milvus 入库时 Ollama embed_documents 每批条数（批量过大易触发客户端异常）",
    )

    # ── 分析节点 LLM（情绪+意图分类，推荐用本地小模型降低延迟）────
    analysis_provider: str = Field(default="openai",     description="analysis_node LLM 提供商：openai 或 ollama")
    analysis_model:    str = Field(default="deepseek-v4-flash", description="analysis_node 使用的模型名")
    analysis_temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description="analysis_node 采样温度（建议低温，保证分类稳定）",
    )

    # ── 生成节点 LLM（回复生成）────────────────────────────────
    generator_model: str = Field(default="deepseek-v4-flash", description="generator_node 使用的模型名")
    generator_temperature: float = Field(
        default=0.05,
        ge=0.0,
        le=2.0,
        description="generator_node 采样温度",
    )

    # ── Milvus 向量数据库 ─────────────────────────────────
    milvus_host: str = Field(default="localhost", description="Milvus 服务地址")
    milvus_port: int = Field(default=19530, description="Milvus 服务端口")
    milvus_user: str = Field(default="", description="Milvus 用户名（可选）")
    milvus_password: str = Field(default="", description="Milvus 密码（可选）")
    milvus_db_name: str = Field(default="default", description="Milvus 数据库名")
    milvus_default_collection: str = Field(default="products")

    # ── PostgreSQL 全文检索库 ─────────────────────────────
    pg_host: str = Field(default="localhost", description="PostgreSQL 主机")
    pg_port: int = Field(default=5432, description="PostgreSQL 端口")
    pg_user: str = Field(default="postgres", description="PostgreSQL 用户名")
    pg_password: str = Field(default="", description="PostgreSQL 密码")
    pg_database: str = Field(default="agent_db", description="PostgreSQL 数据库名")
    pg_pool_min: int = Field(default=10, description="连接池最小连接数")
    pg_pool_max: int = Field(default=50, description="连接池最大连接数")

    # ── BM25 / 分词配置 ───────────────────────────────────
    jieba_user_dict: str = Field(default="", description="jieba 自定义词典路径，为空则不加载")
    bm25_k1: float = Field(default=1.5, description="BM25 词频饱和参数")
    bm25_b: float = Field(default=0.75, description="BM25 文档长度归一化参数")

    # ── RRF 混合检索参数 ──────────────────────────────────
    rrf_k: int = Field(default=60, description="RRF 排序常数，越大越平滑")
    hybrid_vector_weight: float = Field(default=0.6, description="向量检索权重（0~1）")
    hybrid_bm25_weight: float = Field(default=0.4, description="BM25 检索权重（0~1）")
    hybrid_oversampling: int = Field(default=2, description="混合检索各路召回倍数，k*oversampling")
    hybrid_vector_timeout_ms: int = Field(default=3500, description="向量检索超时（毫秒）")
    hybrid_bm25_timeout_ms: int = Field(default=2500, description="BM25 检索超时（毫秒）")

    # ── Redis（可选持久化记忆） ───────────────────────────
    redis_url: str = Field(default="redis://localhost:6379")

    # ── Elasticsearch 外部会话历史 ────────────────────────
    es_host:   str = Field(default="localhost", description="ES 主机")
    es_port:   int = Field(default=9200,        description="ES 端口")
    es_scheme: str = Field(default="http",      description="ES 协议 http/https")
    es_api_key: str = Field(default="",         description="ES API Key（可选）")

    # ── Pipeline 数据清洗 / 分块配置 ─────────────────────
    pipeline_chunk_size: int = Field(default=512, description="分块最大字符数")
    pipeline_chunk_overlap: int = Field(default=64, description="相邻块重叠字符数")
    pipeline_min_chunk_length: int = Field(default=30, description="最小有效块长度，低于此值丢弃")
    pipeline_max_chunk_length: int = Field(default=10000, description="单文档最大字符数，超出截断")
    pipeline_batch_size: int = Field(default=64, description="每批写入检索库的块数")
    pipeline_enable_simplified: bool = Field(default=True, description="是否启用繁简转换")
    pipeline_enable_dedup: bool = Field(default=True, description="是否启用 MD5 去重")

    # ── 路径配置 ──────────────────────────────────────────
    staging_root: str = Field(
        default="./data/staging",
        description="暂存区根目录，支持绝对路径或相对路径。可通过环境变量 STAGING_ROOT 覆盖",
    )
    data_upload_dir: str = Field(
        default="./data/uploads",
        description="文件上传目录，支持绝对路径或相对路径。可通过环境变量 DATA_UPLOAD_DIR 覆盖",
    )

    # ── API 服务 ──────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ── 限流配置 ─────────────────────────────────────────
    rate_limit_enabled: bool = Field(default=True, description="是否启用限流")
    rate_limit_ip_per_minute: int = Field(default=30, description="每 IP 每分钟最大请求数")
    rate_limit_session_per_minute: int = Field(default=20, description="每 session 每分钟最大对话次数")
    rate_limit_global_per_minute: int = Field(default=8000, description="全局每分钟最大请求数")

    # ── API 鉴权 ─────────────────────────────────────────
    api_secret_key: str = Field(default="", description="API 访问密钥，空字符串表示不鉴权")
    api_key_header: str = Field(default="X-API-Key", description="鉴权 Header 名称")

    # ── CRM 对接：静默转人工通知（退货/退款/投诉类命中后 reply 为空，由 CRM 发话）────
    crm_human_transfer_webhook_url: str = Field(
        default="",
        description="POST JSON：event/session_id/user_id/transfer_reason 等；空则仅打结构化日志",
    )

    # ── 后端业务接口（PMS 商品 / OMS 订单，内网无鉴权）────────────
    backend_base_url: str = Field(
        default="https://sales.xinjixiu.com.cn/prod-api",
        description="PMS/OMS 后端 Base URL（不含具体路径）",
    )
    pms_product_list_path: str = Field(
        default="/pms/product/detailList",
        description="商品列表接口路径（相对 backend_base_url）",
    )
    oms_order_base_path: str = Field(
        default="/oms/omsOrder",
        description="订单接口基路径，getOrderList/getLogisticsTrace 在此下拼接（相对 backend_base_url）",
    )

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def warn_missing_key(cls, v: str) -> str:
        """开发环境允许空 key，生产环境会在运行时报错。"""
        return v

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def milvus_uri(self) -> str:
        """拼接 Milvus 连接 URI。"""
        return f"http://{self.milvus_host}:{self.milvus_port}"

    @property
    def pg_dsn(self) -> str:
        """拼接 PostgreSQL DSN（psycopg2 格式）。"""
        return (
            f"host={self.pg_host} port={self.pg_port} "
            f"user={self.pg_user} password={self.pg_password} "
            f"dbname={self.pg_database}"
        )

    @property
    def pg_uri(self) -> str:
        """拼接 PostgreSQL URI（psycopg3 / SQLAlchemy 格式）。"""
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    @property
    def pms_product_list_url(self) -> str:
        """PMS 商品列表接口完整 URL。"""
        return f"{self.backend_base_url.rstrip('/')}{self.pms_product_list_path}"

    @property
    def oms_order_base_url(self) -> str:
        """OMS 订单接口基 URL（不含 /getOrderList/{uid} 等子路径）。"""
        return f"{self.backend_base_url.rstrip('/')}{self.oms_order_base_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例（带缓存）。"""
    return Settings()
