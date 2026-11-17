# AI Customer Service — 生产运维常用命令（GNU Make）
# 使用环境：Linux 生产机、WSL 或 Git Bash（需已安装 docker compose、make）
# 覆盖前请确认 .env 与 compose 服务名与本文件一致（默认 api-1~api-3、postgres）

COMPOSE        ?= docker compose
API            ?= api-1
COMPOSE_FILE   ?= docker-compose.yml
GIT_REF        ?= main
BACKUP_DIR     ?= backups

.PHONY: deploy down restart-api build-update kb-status kb-list-products kb-ingest-products kb-ingest-products-dry kb-ingest-scripts kb-ingest-scripts-dry kb-rebuild kb-rebuild-dry health health-pretty logs backup-pg backup-milvus-report migrate-check migrate-upgrade test test-rag clean-checkpoints

deploy:
	$(COMPOSE) -f $(COMPOSE_FILE) up -d

down:
	$(COMPOSE) -f $(COMPOSE_FILE) down

restart-api:
	$(COMPOSE) -f $(COMPOSE_FILE) restart api-1 api-2 api-3

build-update:
	git fetch origin && git checkout $(GIT_REF) && git pull origin $(GIT_REF)
	$(COMPOSE) -f $(COMPOSE_FILE) build
	$(COMPOSE) -f $(COMPOSE_FILE) up -d --no-deps api-1 api-2 api-3

kb-status:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/kb.py status

kb-list-products:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/kb.py list --collection products

# 产品知识库：路径推断为 products；--category 为 Milvus 元数据必填（或容器环境变量 KB_PRODUCTS_DEFAULT_CATEGORY）
kb-ingest-products:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/kb.py ingest \
		product/新肌秀舒净雅香沐浴乳/新肌秀舒净雅香沐浴乳_知识库.md \
		--category "沐浴乳" --replace

kb-ingest-products-dry:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/kb.py ingest \
		product/新肌秀舒净雅香沐浴乳/新肌秀舒净雅香沐浴乳_知识库.md \
		--category "沐浴乳" --replace --dry-run

kb-ingest-scripts:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/kb.py ingest data/sales_scripts --replace

kb-ingest-scripts-dry:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/kb.py ingest data/sales_scripts --dry-run

kb-rebuild:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/kb.py rebuild --products --scripts

kb-rebuild-dry:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/kb.py rebuild --products --scripts --dry-run

health:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/ops/healthcheck.py

health-pretty:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/ops/healthcheck.py --api-url http://127.0.0.1:8000

logs:
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f api-1 api-2 api-3 --tail 100

backup-pg:
	mkdir -p $(BACKUP_DIR)
	$(COMPOSE) -f $(COMPOSE_FILE) exec -T postgres sh -c 'pg_dump -U "$$POSTGRES_USER" "$$POSTGRES_DB"' \
		> $(BACKUP_DIR)/agent_db_$$(date +%Y%m%d_%H%M%S).sql

backup-milvus-report:
	mkdir -p $(BACKUP_DIR)
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/ops/backup.py milvus-report --out $(BACKUP_DIR)/milvus_report_$$(date +%Y%m%d_%H%M%S).json

migrate-check:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/migrate.py check

migrate-upgrade:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/migrate.py upgrade

clean-checkpoints:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/cleanup_checkpoints.py --inactive-days 7

test:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/test/test_conversation.py
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/test_emotion_boundary.py

test-rag:
	$(COMPOSE) -f $(COMPOSE_FILE) exec $(API) python scripts/test/verify_rag_quality.py
