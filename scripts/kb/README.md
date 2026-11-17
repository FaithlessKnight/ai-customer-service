# 知识库统一 CLI（`scripts/kb.py`）维护说明

本文面向**需要操作或二次开发知识库入库链路**的维护人员：说明入口命令、数据流、`source_file` 语义、与旧脚本的对应关系，以及常见问题。

---

## 1. 定位与目录结构

| 路径 | 作用 |
|------|------|
| `scripts/kb.py` | **唯一推荐入口**：在命令行执行 `python scripts/kb.py …` |
| `scripts/kb/commands.py` | 子命令实现：`status` / `list` / `ingest` / `delete` / `rebuild` / `staging` |
| `scripts/kb/collection_map.py` | 合法 collection 白名单、路径 → collection 推断 |
| `scripts/kb/chunk_tracker.py` | PG（JSONB `metadata`）与 Milvus 按 collection / `source_file` 统计与删除 |
| `scripts/kb/rebuild_std.py` | 全量「删库级」重建 products / sales_script 的完整逻辑 |
| `scripts/rebuild_standard_knowledge.py` | **兼容壳**：只做 `sys.path` 后调用 `kb.rebuild_std.main()` |
| `scripts/ingest_sales_scripts.py` | **兼容壳**：`main()` 委托 `kb.commands.main([...])`；`parse_doc` / `ingest` 仍可被其它代码 import |

业务管道仍在 `agent_framework`：`IngestPipeline`、`HybridRetriever`、`StagingArea`、`TaskRunner` 等；本 CLI 只做编排，不复制分块算法。

---

## 2. 运行前依赖

1. **仓库根目录**：在仓库根执行，或任意目录下写绝对路径调用 `python /path/to/repo/scripts/kb.py`。
2. **环境变量**：与主应用一致（参考根目录 `.env.example`），至少需能连上 **PostgreSQL**、**Milvus**；向量入库需 **Embedding**（如 Ollama `bge-m3`）可用。
3. **Python 路径**：`kb.py` 会自动把**仓库根**和 **`scripts/`** 加入 `sys.path`，无需手动 `PYTHONPATH`。

---

## 3. 子命令速查

在项目根执行（Windows 下路径按本机调整）：

```bash
python scripts/kb.py -h
python scripts/kb.py <子命令> -h
```

### 3.1 `status` — 查看存量

统计 PostgreSQL `documents` 表按 collection 的行数，以及 Milvus 各 collection 的 `num_entities`。

```bash
python scripts/kb.py status
python scripts/kb.py status --collections products,sales_script,faq
```

- 默认 `--collections` 为 `products,sales_script`。
- Milvus 连接失败时仍会打出 PG 结果，Milvus 侧以 WARNING 记录。
- **不要**用 `num_entities` 与 PG 逐行对账：在 `delete` / `ingest --replace` 后它常为**近似值**且可能短期偏高；**以 PG 为准**时请用 **`list --collection`**（按 `source_file` 聚合）。命令执行后会额外打一条 WARNING 提示上述含义。

### 3.2 `list` — 按来源文件聚合

在 PG 中按 `metadata->>'source_file'` 分组，列出每个来源文件对应的 chunk 条数（便于核对「哪个文件占了多少条」）。**delete / replace 后与 Milvus 对账时，优先以本命令的 PG 聚合为准。**

```bash
python scripts/kb.py list --collection products
python scripts/kb.py list --collection sales_script --limit 500
```

### 3.3 `ingest` — 增量入库（单文件或目录）

**`collection=products`（含路径推断为 `products`）时：`--category` 必填**（除非已设置环境变量 `KB_PRODUCTS_DEFAULT_CATEGORY`）。当前 Milvus `products` schema 要求写入 `metadata.category`；缺省会导致**向量插入失败而 PG 仍写入**，造成两侧不一致（测试中已复现）。

```bash
# 产品路径推断为 products：必须带 --category（或与 schema 一致的业务分类）
python scripts/kb.py ingest product/某产品/某产品_知识库.md --category "沐浴乳"
python scripts/kb.py ingest product/某产品/某产品_知识库.md --category "沐浴乳" --replace

# 仅预览
python scripts/kb.py ingest product/某产品/某产品_知识库.md --category "沐浴乳" --dry-run

# 话术（推断为 sales_script）：不需要 --category
python scripts/kb.py ingest data/sales_scripts

# 强制指定 collection（路径无法推断时必须）
python scripts/kb.py ingest D:\docs\foo.md --collection faq
```

**默认 category（可选）**：在 `.env` 或环境中设置 `KB_PRODUCTS_DEFAULT_CATEGORY=沐浴乳` 后，`products` 入库可不写 `--category`；若命令行显式传了 `--category`，仍以命令行为准。

**路径 → collection 推断规则**（定义在 `collection_map.infer_collection`）：

- 路径字符串中含 `data/sales_scripts`，或文件名以 `销售话术.md` 结尾 → `sales_script`
- 路径分段中含 `product`，或路径中含 `/product/`、`\product\` → `products`
- 路径分段中含 `sales_scripts` → `sales_script`
- 以上都不满足 → **必须**显式传 `--collection`（值须在白名单内）

**入库分支**：

| 条件 | 行为 |
|------|------|
| `sales_script` + 目录 | 使用 `ingest_sales_scripts.collect_files`（`*.md`，排除 `README*`），再 `parse_doc` + `ingest` |
| `sales_script` + 单个 `.md` | 同上，仅处理该文件 |
| 其它 collection | `IngestPipeline` + `HybridRetriever`；目录时递归收集 `.md/.txt/.docx/.pdf/.xlsx/.xls/.csv` |

**`--replace`**：对每个待入库文件，先按**文件名** `source_file == <path.name>` 在 Milvus 删除、再在 PG `DELETE … metadata->>'source_file'`，再执行正常入库。请保证元数据里的 `source_file` 与入库文件名一致（产品侧由 `FileLoader` 写入 `path.name`；话术侧由 `parse_doc` 写入 `path.name`）。

**`replace` 与监控**：多次 `replace` 后，Milvus 的 `num_entities` **可能持续上升或短期与 PG 不一致**（删除在存储层多为标记/段合并策略导致，属 Milvus 已知行为）。以 **`list --collection`**（PG 按 `source_file` 聚合）和业务检索为准；若需严谨对齐，以 PG 为准做核对或全量 `rebuild`。按文件删除在实现上使用 **`MilvusClient.delete` + `flush`**，与向量写入侧客户端风格一致，并降低与 LangChain 随机连接 alias 混用时的偶发异常。

**向量写入重试**：`MilvusRetriever.add_documents` / `retrieve` 在检测到 `ConnectionNotExist`、`should create connection` 时会丢弃缓存的 `Milvus` 实例并重试一次，缓解 `replace` 后立即写入时的连接态问题。

**话术目录过滤**（与旧脚本一致）：

```bash
python scripts/kb.py ingest data/sales_scripts --only 01,02
python scripts/kb.py ingest data/sales_scripts --skip 03
```

**`--doc-type` / `--category`**：`--doc-type` 仅作用于 **IngestPipeline** 路径。`--category` 对 **`products` 必填**（或依赖 `KB_PRODUCTS_DEFAULT_CATEGORY`）；其它 collection 按需传入。

### 3.4 `delete` — 删除数据

```bash
# 清空整个 collection：PG 全表删除该 collection + Milvus drop collection
python scripts/kb.py delete --collection products

# 只删某一来源文件对应的块（PG + Milvus）
python scripts/kb.py delete --collection products --source "某产品_知识库.md"

# 干跑：只打日志，不写库
python scripts/kb.py delete --collection products --dry-run
python scripts/kb.py delete --collection products --source "某.md" --dry-run
```

**注意**：无 `--source` 时等价于「整 collection 清空」，务必先 `--dry-run` 或 `status` / `list` 确认。

**删除顺序**（有 `--source` 时）：先 Milvus `delete(expr)`，再 PG `DELETE`，与 `ingest --replace` 的删除顺序对齐，减少「向量已删、PG 仍残留」的窗口问题。

### 3.5 `rebuild` — 全量重建（危险操作）

透传所有参数给 `rebuild_std.main()`（内部仍用 `argparse` 读 `sys.argv`）。

```bash
python scripts/kb.py rebuild --dry-run --products --scripts
python scripts/kb.py rebuild --products
python scripts/kb.py rebuild --scripts
python scripts/kb.py rebuild --products --product-docx "D:\path\产品.docx"
```

**必须**显式指定 `--products` 和/或 `--scripts`（或旧版兼容的 `--skip-products` / `--skip-scripts` 组合），否则进程会以非零退出码退出并提示错误，**避免误删全库**。

`--dry-run`：只打印当前 PG/Milvus 存量与计划，不执行 `DELETE` / `drop` / 入库。

产品源：优先 `product/*/*_知识库.md`（跳过 `_` 开头目录）；若无则 `--product-docx` 或桌面启发式 docx。话术源：固定为仓库内 `data/sales_scripts`。

### 3.6 `staging` — 暂存区（非技术同事可用）

依赖 `agent_framework.config.settings` 中的暂存根目录（如 `data/staging`）。

```bash
python scripts/kb.py staging upload path/to/文件.docx --collection products --doc-type product --category "沐浴乳"
python scripts/kb.py staging list
python scripts/kb.py staging process
python scripts/kb.py staging process --dry-run --preview-chunks 10
```

- `upload`：复制到 `pending/`，写入元数据中的 `collection`、`doc_type`；**`collection=products` 时必须带 `--category`**（或已设置 `KB_PRODUCTS_DEFAULT_CATEGORY`），否则 `process` 阶段向量写入易失败。
- `list`：调用 `StagingArea.print_status()`。
- `process`：默认 `TaskRunner.run_all()`；`--dry-run` 时对最多 20 个 pending 文件调用 `TaskRunner.dry_run`（不入库）。

**说明**：默认 `TaskRunner` 未显式传入 `HybridRetriever` 时会走内存 BM25 警告分支；若生产环境依赖 PG/Milvus 一致写入，建议在后续迭代中为 CLI 的 `TaskRunner` 注入与 `ingest` 相同的 `HybridRetriever`（当前与 API 侧行为可能不一致，维护时需注意）。

---

## 4. `source_file` 与维护约定

- **PostgreSQL**：`documents.metadata` 为 JSONB；按文件删除/替换使用 `metadata->>'source_file'`。
- **Milvus**：动态字段 `source_file`；删除表达式形如 `source_file == "某.md"`（实现中对 `"`、`\` 做了转义）。

维护时约定：**同一逻辑文档多次入库，文件名应稳定**，否则 `--replace` / `delete --source` 无法对齐旧数据。

---

## 5. 与旧命令对照

| 旧方式 | 推荐等价方式 |
|--------|----------------|
| `python scripts/rebuild_standard_knowledge.py --products --scripts` | `python scripts/kb.py rebuild --products --scripts` |
| `python scripts/ingest_sales_scripts.py --source data/sales_scripts` | `python scripts/kb.py ingest data/sales_scripts` |
| 旧脚本仍保留，内部已委托 `kb`，便于 CI/文档过渡期不改命令行。 | |

---

## 6. 常见问题

**`ingest` 报「products 时必须提供 --category」**  
当前 Milvus `products` 要求 `metadata.category`。请加上 `--category "沐浴乳"`（或与 schema 一致的值），或在环境中设置 `KB_PRODUCTS_DEFAULT_CATEGORY`。

**`status` 里 Milvus 条数比 PG 多、replace 后还在涨**  
多为 `num_entities` 的近似统计，不代表未删除。请以 **`list --collection`** 的 PG 聚合为准。

**PG 有数据，Milvus 为 0 或明显偏少**  
多为 Embedding 失败或 Milvus 写入失败。先查日志与 Ollama/OpenAI 配置；可对单 collection 再执行 `ingest` 或针对性 `rebuild`。

**`ingest` 提示无法推断 collection**  
路径不在 `product`、`sales_scripts` 等规则内，请显式 `--collection <白名单中的值>`。

**`delete --source` 删不干净**  
确认 PG 里该条目的 `metadata->>'source_file'` 与传入字符串完全一致（含扩展名、大小写是否与入库时一致）。

**全量 `rebuild` 误执行**  
脚本已要求必须带 `--products` / `--scripts`；执行前务必加 `--dry-run` 看计划。

---

## 7. 扩展开发提示

- 新增子命令：在 `commands.py` 的 `build_parser()` 注册，并实现 `cmd_*`，最后 `set_defaults(func=…)`。
- 新增可推断路径：改 `collection_map.infer_collection`，并同步本文档「推断规则」一节。
- 新增 collection：同时更新 `VALID_COLLECTIONS`、API  ingest 白名单（若存在）、以及 Milvus/PG 侧建表或 collection 创建逻辑（若有）。

更简略的索引见仓库根下 `scripts/README.md` 中的「知识库统一 CLI」小节。
