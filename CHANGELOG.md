# Changelog

All notable changes to the Smart Research Agent (SRA) project will be documented in this file.

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
- **Neo4j Knowledge Graph**: Triple extraction and Cypher query persistence.
- **Hybrid Search Engine**: Combined lexical (BM25) and dense embeddings vector search (ChromaDB) with Reciprocal Rank Fusion (RRF) and Cohere Reranking.
- **Celery & Redis Worker**: Asynchronous queue processing infrastructure.
- **Multilingual Searcher**: Dynamic query translation via LLM.
- **OCR & Document Parsers**: PDF tables and text extraction using pdfplumber and Tesseract OCR.
- **Video Transcriber**: yt-dlp audio downloader and Whisper transcriptions.
- **Firecrawl Agent Mode**: Support for domain maps, automated interactions, and batch scraping.
