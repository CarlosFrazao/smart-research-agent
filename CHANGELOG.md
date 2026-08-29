# Changelog

All notable changes to the Smart Research Agent (SRA) project will be documented in this file.

## [1.0.0] - 2026-08-28 — Audit Corrections (P0–P3)

### Fixed (Post-Audit — 2026-08-29)
- **P1-11**: `src/dependencies.py` — replaced deprecated `@app.on_event("startup"/"shutdown")` in `setup_fastapi()` with `@asynccontextmanager lifespan` pattern; added `setup_fastapi_lifespan()` method returning a lifespan context manager.

### Fixed
- **P0-1**: `requirements.txt` — added `; sys_platform == "win32"` platform marker to `pywin32==312`, making the file installable on Linux. CI now runs `pip install -r requirements.txt` on `ubuntu-latest`.
- **P0-2**: Fail-fast in production — `SRA_ENV=production` now requires `SRA_API_KEY` and rejects `CORS_ALLOWED_ORIGINS="*"`. Process refuses to start via `SystemExit` if conditions are not met.
- **P0-3**: Added `LICENSE` file (MIT) with copyright holder from git config.
- **P0-4**: Added "Production Deployment" section to README.md documenting env-var injection via Docker/K8s secrets.
- **P0-5**: Hardened `docker-compose.yml` — Redis now requires password (`REDIS_PASSWORD`), ChromaDB has auth configuration documented, Grafana no longer has a plaintext default password.
- **P0-6**: Rate limit is now configurable via `SRA_RATE_LIMIT` env var (default `"10/minute"`).

### Changed
- `src/config.py` — `sra_env` field defaults to `"development"`; CORS default is `["*"]` in dev, `[]` in production.
- `src/mcp_server.py` — replaced deprecated `@app.on_event` with `@asynccontextmanager lifespan`; calls `validate_production()` on startup.
- `api/main.py` — lifespan calls `validate_production()`; rate limit decorators read from `SRA_RATE_LIMIT`.
- `requirements.txt` — 312 entries audited; `pywin32` platform marker added; `colorama`, `concurrent-log-handler`, `tiktoken` confirmed present.

### CI
- `ci.yml` — added `install-check` job (pip install -r requirements.txt on ubuntu-latest); test job now uses `-m "not integration"` to exclude integration tests; added nightly `integration` job on schedule.

### Documentation
- `.env.example` — added `SRA_ENV`, `SRA_RATE_LIMIT`, `REDIS_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`, `CHROMA_AUTH_SECRET`; updated CORS and Redis URL comments for production.

### Backend Neo4j (Legado Opcional)
O backend Neo4j foi substituído pelo KuzuDB (v6.2.0) como padrão.
As referências ao Neo4j mantidas no código são intencionais e habilitadas
via Docker Compose profile `neo4j` para usuários que precisam de compatibilidade.
Para ativar: `docker-compose --profile neo4j up`

## [6.2.0] - 2026-07-07

### Changed
- **Knowledge Graph Unification (KuzuDB)**: Removed the dual Neo4j backend, consolidating all knowledge graph persistence and query operations on KuzuDB as the single definitive engine. `src/memory/knowledge_graph.py` (legacy `KnowledgeGraph`) now inherits from `SemanticKnowledgeGraph` and operates on the local embedded KuzuDB file instead of a remote Neo4j server.
- **Documentation Alignment**: Updated `README.md`, `docker-compose.yml` and related docs to reflect KuzuDB as the official graph backend (no external Neo4j server required).
- **Config Cleanup**: Deprecated `neo4j_uri`, `neo4j_user` and `neo4j_password` settings. The system now uses `KUZU_DATA_PATH` (default `kuzu_data/`) for local graph storage.

## [6.0.0] - 2026-07-03

### Added
- **FastAPI REST API**: Sync and Async polling endpoints with lifespan and CORS middleware configuration.
- **Streamlit Web UI**: Visual control dashboard with research presets, limits, and multi-language support.
- **Typer CLI**: Standardized command line interface featuring rich terminal markdown rendering and spinners.
- **Prometheus Telemetries**: Counters, Histograms, and Gauges monitoring searches, duration, cache hits, token usage, and circuit breakers status on port 8001.
- **JSON Structured Logging**: Configured `structlog` output stream with ISO timestamp and contextvars merging.
- **E2E Integration Testing**: Full mock and real pipeline tests, cache TTL validation, circuit breaker scenarios, and prompt injection filters.

## [5.0.0] - 2026-07-02

### Added
- **Neo4j Knowledge Graph**: Triple extraction and Cypher query persistence ( migrou para KuzuDB na versão 6.2.0).
- **Hybrid Search Engine**: Combined lexical (BM25) and dense embeddings vector search (ChromaDB) with Reciprocal Rank Fusion (RRF) and Cohere Reranking.
- **Celery & Redis Worker**: Asynchronous queue processing infrastructure.
- **Multilingual Searcher**: Dynamic query translation via LLM.
- **OCR & Document Parsers**: PDF tables and text extraction using pdfplumber and Tesseract OCR.
- **Video Transcriber**: yt-dlp audio downloader and Whisper transcriptions.
- **Firecrawl Agent Mode**: Support for domain maps, automated interactions, and batch scraping.
