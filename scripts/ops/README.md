# ops 分类

用于运维、迁移、环境维护、备份与健康巡检。

**专人维护总览（命令大全）**：[docs/运维手册.md](../../docs/运维手册.md)

## 脚本索引

| 脚本 | 说明 |
|------|------|
| `migrate.py`（根目录） | Alembic 迁移封装：`check` / `upgrade` / `current` 等 |
| `ops/migrate.py` | 转调根目录 `migrate.py`，便于分类引用 |
| `cleanup_checkpoints.py` | LangGraph checkpoint 清理 |
| `deploy.sh` | 部署辅助 |
| **`ops/healthcheck.py`** | 一键巡检 PG / Milvus / Redis / Ollama，**JSON 输出**（退出码 0=全通过，1=存在 error） |
| **`ops/backup.py`** | `pg-dump` / `pg-restore`（docker compose exec postgres）· `milvus-report`（集合元数据 JSON，**非**向量全量备份） |

## 生产环境命令方案（摘要）

### Docker Compose（服务名与 `docker-compose.yml` 一致）

- 启动：`docker compose up -d`
- 停服务保留卷：`docker compose down`
- 仅重启 API：`docker compose restart api-1 api-2 api-3`
- 更新镜像后滚动 API：`docker compose build && docker compose up -d --no-deps api-1 api-2 api-3`
- 日志：`docker compose logs -f api-1 api-2 api-3 --tail 100`

### 知识库（容器内，工作目录 `/app`）

```bash
docker compose exec api-1 python scripts/kb.py status
docker compose exec api-1 python scripts/kb.py list --collection products
docker compose exec api-1 python scripts/kb.py ingest product/.../xxx_知识库.md --category "<Milvus元数据标签>" --replace
docker compose exec api-1 python scripts/kb.py ingest data/sales_scripts --replace
docker compose exec api-1 python scripts/kb.py rebuild --products --scripts --dry-run
```

详见 `scripts/kb/README.md`。

### 一键巡检

```bash
# 容器内（推荐，与生产配置一致）
docker compose exec api-1 python scripts/ops/healthcheck.py

# 带 API 自身 /health（容器内需能访问自身监听地址）
docker compose exec api-1 python scripts/ops/healthcheck.py --api-url http://127.0.0.1:8000
```

### 备份

- **PG 逻辑备份**（宿主机 Makefile：`make backup-pg`）或 `backup.py pg-dump`。
- **Milvus**：向量全量依赖 **Docker 卷快照** 或 Milvus 官方备份链；`backup.py milvus-report` 仅导出集合与 `get_collection_stats` 便于留痕、对账。

```bash
docker compose exec api-1 python scripts/ops/backup.py milvus-report --out backups/milvus_report.json
```

**pg-restore 会覆盖当前库数据**，执行前务必备份。

### Makefile（仓库根）

封装高频目标：`make deploy`、`make kb-status`、`make kb-ingest-products-dry`、`make health`、`make backup-pg` 等。需 **GNU Make + bash 环境**（Linux / WSL / Git Bash）。

### 迁移与清理

```bash
docker compose exec api-1 python scripts/migrate.py check
docker compose exec api-1 python scripts/migrate.py upgrade
docker compose exec api-1 python scripts/cleanup_checkpoints.py --inactive-days 7 --dry-run
```

### 质量验证

```bash
docker compose exec api-1 python scripts/test/verify_rag_quality.py
docker compose exec api-1 python scripts/test/test_conversation.py
docker compose exec api-1 python scripts/test_emotion_boundary.py
```

（`test_emotion_boundary.py` 位于 `scripts/` 根目录，与 `scripts/test/` 下脚本并列。）
