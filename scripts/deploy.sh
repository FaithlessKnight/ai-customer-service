#!/bin/bash
# ============================================================
# AI Customer Service - 云端部署脚本
# 适用于：Ubuntu 22.04 / Debian 12 云服务器
# ============================================================
# 使用方式：
#   chmod +x scripts/deploy.sh
#   ./scripts/deploy.sh          # 首次部署
#   ./scripts/deploy.sh update   # 更新服务（不重建依赖服务）
#   ./scripts/deploy.sh rollback # 回滚到上一版本
# ============================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

MODE=${1:-deploy}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"\u0026\u0026 pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

info "工作目录: $ROOT_DIR  模式: $MODE"

# ── 检查依赖 ──────────────────────────────────────────────
check_deps() {
    info "检查系统依赖..."
    for cmd in docker git curl; do
        command -v "$cmd" \u003e/dev/null 2\u003e\u00261 || error "未找到 $cmd，请先安装"
    done
    docker compose version \u003e/dev/null 2\u003e\u00261 || error "需要 docker compose v2，请升级 Docker"
    ok "依赖检查通过"
}

# ── 检查 .env 配置 ─────────────────────────────────────────
check_env() {
    info "检查环境变量配置..."
    [[ -f .env ]] || error ".env 文件不存在，请先执行: cp .env.example .env 并填入配置"
    
    local missing=()
    while IFS= read -r line; do
        [[ "$line" =~ ^#.*$ || -z "$line" ]] \u0026\u0026 continue
        key="${line%%=*}"
        val="${line#*=}"
        [[ -z "$val" || "$val" == *"your-"* || "$val" == *"-here"* ]] \u0026\u0026 missing+=("$key")
    done \u003c .env
    
    if [[ ${#missing[@]} -gt 0 ]]; then
        warn "以下配置项未填写：${missing[*]}"
        warn "部分功能可能不可用，确认继续？(y/N)"
        read -r confirm
        [[ "$confirm" =~ ^[Yy]$ ]] || exit 1
    fi
    ok "环境变量检查完成"
}

# ── 创建必要目录 ───────────────────────────────────────────
setup_dirs() {
    info "创建数据目录..."
    mkdir -p data/staging data/uploads data/personas deploy/certs
    chmod 755 data data/staging data/uploads
    ok "目录创建完成"
}

# ── 拉取 Ollama 模型 ───────────────────────────────────────
pull_ollama_model() {
    local model
    model=$(grep '^EMBEDDING_MODEL=' .env | cut -d= -f2)
    model=${model:-bge-m3}
    info "拉取 Ollama Embedding 模型: $model ..."
    docker compose exec -T ollama ollama pull "$model" || warn "Ollama 模型拉取失败，请手动执行: docker compose exec ollama ollama pull $model"
    ok "Ollama 模型就绪"
}

# ── 运行数据库迁移 ─────────────────────────────────────────
run_migrations() {
    info "运行数据库迁移..."
    sleep 5  # 等待 PostgreSQL 就绪
    docker compose exec -T api python scripts/migrate.py upgrade || warn "迁移失败，请检查数据库连接"
    ok "数据库迁移完成"
}

# ── 首次部署 ──────────────────────────────────────────────
deploy() {
    check_deps
    check_env
    setup_dirs
    
    info "构建并启动所有服务..."
    docker compose pull --ignore-pull-failures 2\u003e/dev/null || true
    docker compose build --no-cache api
    docker compose up -d
    
    pull_ollama_model
    run_migrations
    
    info "等待服务启动..."
    local retries=0
    until curl -sf http://localhost:8000/health >/dev/null 2>&1; do
        retries=$((retries+1))
        [[ $retries -ge 24 ]] && error "服务启动超时（>120s），请查看日志: docker compose logs api"
        info "等待 API 就绪... ($retries/24)"
        sleep 5
    done
    
    ok "====== 部署成功 ======"
    echo -e "${GREEN}"
    echo "  API:      http://\$(hostname -I | awk '{print \$1}'):8000"
    echo "  Admin:    http://\$(hostname -I | awk '{print \$1}'):8000/admin/"
    echo "  Ingest:   http://\$(hostname -I | awk '{print \$1}'):8000/admin/ingest"
    echo "  API Docs: http://\$(hostname -I | awk '{print \$1}'):8000/docs"
    echo -e "${NC}"
}

# ── 滚动更新（保持依赖服务运行）────────────────────────────
update() {
    check_deps
    check_env
    
    info "拉取最新代码..."
    git pull origin main
    
    info "重新构建 API 镜像..."
    docker compose build api
    
    info "滚动重启 API 服务..."
    docker compose up -d --no-deps api
    
    sleep 15
    run_migrations
    
    # 验证
    curl -sf http://localhost:8000/health \u003e/dev/null 2\u003e\u00261 \
        \u0026\u0026 ok "更新成功" \
        || error "更新后健康检查失败，请查看日志: docker compose logs api"
}

# ── 回滚 ──────────────────────────────────────────────────
rollback() {
    warn "回滚到上一个镜像版本..."
    docker compose stop api
    docker tag ai-customer-service-api:latest ai-customer-service-api:rollback-failed 2\u003e/dev/null || true
    # 使用 git 回滚代码
    git log --oneline -5
    echo "请输入要回滚到的 commit hash:"
    read -r commit
    git checkout "$commit" -- .
    docker compose build api
    docker compose up -d --no-deps api
    ok "回滚完成，请验证服务状态"
}

# ── 入口 ──────────────────────────────────────────────────
case "$MODE" in
    deploy)   deploy ;;
    update)   update ;;
    rollback) rollback ;;
    *) echo "用法: $0 [deploy|update|rollback]"; exit 1 ;;
esac
