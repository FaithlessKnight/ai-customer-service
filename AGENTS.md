# AGENTS.md

This file provides guidance to Codex (claude.ai/code) when working with code in this repository.

## Quick commands

```bash
# Development server
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

# Docker compose (3 API instances + Nginx + all dependencies)
docker compose up -d
docker compose logs -f api-1 api-2 api-3

# Run tests (inside container)
docker compose exec api-1 python scripts/test/test_conversation.py
docker compose exec api-1 python scripts/test/verify_rag_quality.py

# Install dependencies
pip install -r requirements.txt
```

Makefile targets (Linux/WSL/Git Bash, inside the repo with `docker compose` available):
- `make deploy` / `make down` / `make restart-api` / `make build-update`
- `make kb-status` / `make kb-rebuild` / `make kb-ingest-products`
- `make health` / `make logs` / `make backup-pg`
- `make migrate-check` / `make migrate-upgrade`

## Architecture overview

### Two AI agents

| Agent | Endpoint | Graph | State |
|-------|----------|-------|-------|
| Customer Service | `POST /cs`, `POST /cs/outreach` | analysis → route → tools/rag/silent_transfer → prompt_builder → generator | `CustomerServiceState` (MessagesState subclass) |
| Moments (朋友圈) | `POST /moments` | emotion → (positive?) rag → generate | `MomentsState` (plain TypedDict) |

The **CustomerServiceAgent** has two compiled subgraphs sharing one checkpointer:
- **Chat graph** (`/cs`): Full pipeline with emotion/intent analysis, tool calling (order/logistics queries), hybrid RAG retrieval, and response generation.
- **Outreach graph** (`/cs/outreach`): CRM-initiated proactive messaging. Skips analysis — `outreach_init` injects fixed calm emotion + knowledge intent, then `rag(sales_script) → prompt_builder → generator`.

### Graph execution flow (chat)

```
analysis_node (single LLM call: emotion + intent + profile extraction)
    │
    ▼ route_after_analysis()
    │
    ├─ need_human_transfer → silent_transfer → END
    ├─ order + knowledge   → [tools || rag] (parallel Send)
    ├─ order only          → tools
    ├─ knowledge only      → rag
    └─ chitchat/unknown    → prompt_builder (skip retrieval)
                                    │
                                    ▼
                              generator → END
```

### Agent framework layer (`agent_framework/`)

Reusable infrastructure, not business-specific:
- `graph/builder.py` — Fluent chain API over LangGraph `StateGraph`
- `state/base.py` — `BaseAgentState` TypedDict with messages, context, routing fields
- `agents/base.py` — `BaseAgent` ABC with `invoke/stream/chat`
- `config/settings.py` — Pydantic `Settings` from `.env`, cached via `get_settings()`
- `config/llm_factory.py` — `create_llm(provider, model, ...)` → `BaseChatModel`
- `memory/hybrid_retriever.py` — RRF fusion of Milvus (vector) + BM25 (keyword), with timeouts and graceful degradation
- `memory/vector_store.py`, `memory/bm25_retriever.py`, `memory/pg_client.py` — Individual retrievers
- `memory/checkpointer.py`, `memory/conversation.py` — PG/Redis/Memory checkpointers
- `pipeline/ingest_pipeline.py` — Document ingestion: loader → cleaner → splitter → retriever

### Customer Service state (`agents/customer_service/state.py`)

The `CustomerServiceState` (extends `MessagesState`) holds all node inputs/outputs in flat TypedDict fields organized by phase:
- **[Session]**: `user_input`, `session_id`, `user_id`, `persona_config`, `knowledge_collections`
- **[Emotion]**: `emotion`, `emotion_polarity`, `emotion_score`, `need_human_transfer`, `transfer_reason`
- **[Intent]**: `intent`, `intents`, `intent_score`, `intent_slots`, `wants_purchase_link`
- **[Tools]**: `order_result`, `tool_error`
- **[Retrieval]**: `retrieved_chunks`, `retrieval_query`
- **[Generation]**: `final_reply`, `reply_parts`, `phase_summaries`, `master_summary`
- **[CRM lifecycle]**: `scene`, `journey_stage`, `intent_level`, `customer_profile`, `intent_level_delta`, `next_stage_suggestion`, `field_updates`
- **[Control]**: `debug_info` (Annotated reducer merges parallel writes), `_is_outreach` flag

### LLM configuration

The project uses two separate LLM instances:
- **Analysis LLM** (`analysis_model`, `analysis_temperature=0.0`): Emotion + intent classification with structured output
- **Generator LLM** (`generator_model`, `generator_temperature=0.05`): Response generation

Configured via `OPENAI_BASE_URL` (DashScope-compatible) or `ANTHROPIC_API_KEY`. The analysis node uses `with_structured_output` for guaranteed schema output.

### Retrieval architecture

Hybrid RRF retrieval: `HybridRetriever` runs Milvus vector search and PostgreSQL BM25 keyword search in parallel, then fuses results with weighted Reciprocal Rank Fusion. Each path has independent timeouts — if one fails, the other's results are returned alone. Embedding uses local Ollama `bge-m3` (1024-dim). Multiple knowledge base collections are supported (`products`, `faq`, `sop`, `policy`, `sales_script`, `moments_knowledge`, `cs_knowledge`).

### Session persistence

Checkpointer priority chain: Redis → PostgreSQL → Memory (in-memory). `CustomerServiceAgent` uses async checkpointer (`ainvoke` with `durability="exit"`). The PG checkpointer uses `AsyncPostgresSaver` with a connection pool. On connection errors, the agent auto-rebuilds the checkpointer and retries once.

### API layer

- `api/main.py` — FastAPI app, CORS, rate limiting, lifespan (warmup agents, start Milvus keepalive)
- `api/routers/cs.py` — `POST /cs`, `POST /cs/outreach`, `GET /cs/debug/{session_id}`
- `api/routers/moments.py` — `POST /moments` (emotion → like decision + comment)
- `api/routers/ingest.py` — File upload, staging management, knowledge base ingestion
- `api/routers/admin.py` — Bot/persona CRUD management
- `api/dependencies.py` — FastAPI `Depends` singletons for agents, pipeline, staging, task runner

### Infrastructure (docker-compose)

3 API instances behind Nginx, PostgreSQL 16, Redis 7, Milvus standalone (etcd + MinIO), Ollama for embeddings. All services health-checked.

### CRM integration

`/cs` and `/cs/outreach` accept CRM lifecycle fields (`scene`, `journey_stage`, `intent_level`, `customer_profile`). The agent returns `field_updates`, `intent_level_delta`, `next_stage_suggestion` for CRM to persist. Silent transfer on `transfer_reason=after_sales` sends empty `reply`/`reply_parts` — CRM takes over messaging. See `docs/journey_stage_spec.md` for the full CRM-agent field contract.
