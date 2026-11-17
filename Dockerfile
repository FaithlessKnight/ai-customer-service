# ============================================================
# AI Customer Service - Dockerfile
# 多阶段构建：builder 安装依赖，runtime 运行服务
# ============================================================

# ── Stage 1: 依赖构建 ────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# 安装编译依赖（psycopg2-binary 等需要 gcc）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# 先复制 requirements，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: 运行时镜像 ──────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL maintainer="AI Customer Service"
LABEL description="LangGraph AI Customer Service API"

WORKDIR /app

# 运行时系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 复制已安装的 Python 包
COPY --from=builder /install /usr/local

# 复制项目代码
COPY . .

# 创建非 root 用户运行服务
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/data/staging /app/data/uploads /app/data && \
    chown -R appuser:appuser /app

USER appuser

# 数据目录挂载点
VOLUME ["/app/data"]

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 启动命令（生产用 4 个 worker）
CMD ["uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--loop", "uvloop", \
     "--access-log"]
