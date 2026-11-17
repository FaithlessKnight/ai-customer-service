# Scripts 分类索引

为避免一次性迁移破坏现有命令，采用“**分类入口 + 根目录兼容**”方案：

- 推荐从分类目录执行（便于管理）
- 根目录原脚本保留，兼容历史命令

## 知识库统一 CLI（推荐）

单入口 `scripts/kb.py`，覆盖状态查询、按来源列出、增量/替换入库、按文件或整库删除、全量重建、暂存区。

```text
python scripts/kb.py status
python scripts/kb.py list --collection products
python scripts/kb.py ingest <路径> --category "沐浴乳" [--collection …] [--replace] [--dry-run]
python scripts/kb.py delete --collection products [--source "某.md"] [--dry-run]
python scripts/kb.py rebuild --products --scripts [--dry-run]
python scripts/kb.py staging upload <文件> --collection products --category "沐浴乳"
python scripts/kb.py staging list
python scripts/kb.py staging process [--dry-run]
```

- 路径推断为 `products` 时 **`--category` 必填**（或配置 `KB_PRODUCTS_DEFAULT_CATEGORY`），避免 Milvus 写入失败而 PG 仍插入。
- 未指定 `--collection` 时：`data/sales_scripts` → `sales_script`，`product/…` → `products`。
- `--replace`：先按 `metadata.source_file` / Milvus 字段 `source_file` 删旧块再写入；**不要用 `status` 的 Milvus 条数验收 replace**，请用 `list --collection`。
- `scripts/rebuild_standard_knowledge.py`、`scripts/ingest_sales_scripts.py` 仍可用，内部委托 `kb` 模块，与上述命令等价。

维护说明（子命令详解、数据流、`source_file` 约定、排错）：[scripts/kb/README.md](kb/README.md)

**运维命令总览（Docker / Makefile / 巡检 / 备份）**：[docs/运维手册.md](../docs/运维手册.md)

## ingest（入库与知识构建）

- `scripts/kb.py`（统一入口）
- `scripts/ingest_sales_scripts.py`（委托 `kb ingest`）
- `scripts/rebuild_standard_knowledge.py`（委托 `kb.rebuild_std`）
- `scripts/ingest/ingest_sales_scripts.py`
- `scripts/ingest/rebuild_standard_knowledge.py`
- `scripts/ingest/desktop_scripts_add_metadata.py`

## test（功能/质量测试）

- `scripts/test/test_conversation.py`
- `scripts/test/test_qa_quality.py`
- `scripts/test/test_security.py`
- `scripts/test/verify_rag_quality.py`
- `scripts/test/test_link_and_product_name.py`

## benchmark（性能评测）

- `scripts/benchmark/benchmark_e2e.py`
- `scripts/benchmark/benchmark_models.py`
- `scripts/benchmark/benchmark_analysis_vs_generator.py`

## ops（运维与维护）

- 仓库根 **`Makefile`**：`make kb-status`、`make health`、`make backup-pg` 等（需 GNU Make + Docker）
- `scripts/ops/healthcheck.py` — 一键巡检 JSON
- `scripts/ops/backup.py` — PG dump/restore（compose）、Milvus 元数据报告
- `scripts/ops/migrate.py`
- `scripts/ops/cleanup_checkpoints.py`
- `scripts/ops/deploy.sh`

## 示例

- `python scripts/kb.py rebuild --products --scripts --dry-run`
- `python scripts/test/test_conversation.py`
- `python scripts/benchmark/benchmark_e2e.py --concurrency 10 20`
- `bash scripts/ops/deploy.sh`

## 建议规范（后续）

- 新脚本命名：`<domain>_<purpose>.py`（例如 `ingest_products.py`）。
- 新脚本顶部增加用途说明、输入参数示例、依赖前置条件。
- 主体脚本后续可逐步迁入子目录；在确认 CI/文档已切换前，保留根目录兼容入口。
