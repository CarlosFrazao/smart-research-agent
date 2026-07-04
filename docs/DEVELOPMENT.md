# DEVELOPMENT.md — Guia de Desenvolvimento do Smart Research Agent

> **Versão:** 6.0  
> **Última atualização:** 2026-07-04  
> **Público-alvo:** Contribuidores, mantenedores e integradores

---

## 📋 Índice

1. [Setup Local](#1-setup-local)
2. [Arquitetura do Projeto](#2-arquitetura-do-projeto)
3. [Como Adicionar um Novo Searcher](#3-como-adicionar-um-novo-searcher)
4. [Como Adicionar um Novo Stage](#4-como-adicionar-um-novo-stage)
5. [Como Rodar Testes](#5-como-rodar-testes)
6. [Como Medir Custo e Latência](#6-como-medir-custo-e-latência)
7. [Referência Rápida](#7-referência-rápida)

---

## 1. Setup Local

### 1.1 Pré-requisitos

| Ferramenta | Versão Mínima | Propósito |
|------------|--------------|-----------|
| Python | 3.11 | Runtime principal |
| Docker | 24.x | Serviços auxiliares (Redis, ChromaDB, Neo4j) |
| Docker Compose | 2.x | Orquestração de containers |
| Git | 2.40 | Controle de versão |

### 1.2 Clone e Instalação

```bash
# Clone o repositório
git clone https://github.com/CarlosFrazao/smart-research-agent.git
cd smart-research-agent

# Crie ambiente virtual (recomendado)
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate  # Windows

# Instale dependências
pip install -e ".[dev]"

# Instale dependências opcionais (tracing, learned ranker)
pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp
pip install lightgbm scikit-learn sentence-transformers
```

### 1.3 Docker Compose para Desenvolvimento

Crie `docker-compose.dev.yml` na raiz do projeto:

```yaml
version: '3.8'

services:
  # --- Aplicação Principal ---
  sra-dev:
    build: .
    env_file: .env
    environment:
      - FIRECRAWL_BASE_URL=http://firecrawl-api-new:3002
      - CHROMADB_HOST=chromadb
      - CHROMADB_PORT=8000
      - SEARXNG_URL=http://firecrawl-searxng:8080
      - REDIS_URL=redis://redis:6379/0
      - NEO4J_URI=bolt://neo4j:7687
      - JAEGER_ENDPOINT=http://jaeger:4317
    ports:
      - "3458:3458"   # FastAPI
      - "8001:8001"   # Prometheus metrics
    volumes:
      - ./reports:/app/reports
      - ./.cache:/app/.cache
      - ./kuzu_data:/app/kuzu_data
      - ./src:/app/src          # Hot reload
      - ./tests:/app/tests
      - ./prompts:/app/prompts
      - ./hooks:/app/hooks
      - ./static:/app/static
    extra_hosts:
      - "host.docker.internal:host-gateway"
    networks:
      - sra-net
    depends_on:
      redis:
        condition: service_healthy
      chromadb:
        condition: service_healthy
      neo4j:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:3458/health || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 30s
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 1G

  # --- ChromaDB (Vector Store) ---
  chromadb:
    image: chromadb/chroma:latest
    container_name: sra-chromadb-dev
    ports:
      - "3024:8000"
    volumes:
      - ./chroma_data:/chroma/data
    environment:
      - IS_PERSISTENT=TRUE
      - PERSIST_DIRECTORY=/chroma/data
      - ANONYMIZED_TELEMETRY=FALSE
    networks:
      - sra-net
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:8000/api/v1/heartbeat || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 3

  # --- Redis (Cache + Celery Broker) ---
  redis:
    image: redis:7-alpine
    container_name: sra-redis-dev
    ports:
      - "6379:6379"
    volumes:
      - ./redis_data:/data
    command: redis-server --appendonly yes
    networks:
      - sra-net
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  # --- Neo4j (Graph Database) ---
  neo4j:
    image: neo4j:5-community
    container_name: sra-neo4j-dev
    ports:
      - "7474:7474"  # HTTP
      - "7687:7687"  # Bolt
    volumes:
      - ./neo4j_data:/data
    environment:
      - NEO4J_AUTH=neo4j/devpassword123
      - NEO4J_PLUGINS=["apoc"]
      - NEO4J_dbms_memory_heap_max__size=512M
    networks:
      - sra-net
    healthcheck:
      test: ["CMD-SHELL", "cypher-shell -u neo4j -p devpassword123 'RETURN 1' || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s

  # --- Jaeger (Distributed Tracing) ---
  jaeger:
    image: jaegertracing/all-in-one:latest
    container_name: sra-jaeger-dev
    ports:
      - "16686:16686"  # UI
      - "4317:4317"    # OTLP gRPC
      - "4318:4318"    # OTLP HTTP
    environment:
      - COLLECTOR_OTLP_ENABLED=true
    networks:
      - sra-net

  # --- Prometheus (Metrics) ---
  prometheus:
    image: prom/prometheus:latest
    container_name: sra-prometheus-dev
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
    networks:
      - sra-net

  # --- Grafana (Dashboards) ---
  grafana:
    image: grafana/grafana:latest
    container_name: sra-grafana-dev
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
    volumes:
      - ./grafana_data:/var/lib/grafana
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
    networks:
      - sra-net
    depends_on:
      - prometheus

networks:
  sra-net:
    driver: bridge
```

### 1.4 Inicialização do Ambiente

```bash
# Subir todos os serviços
docker-compose -f docker-compose.dev.yml up -d

# Verificar status
docker-compose -f docker-compose.dev.yml ps

# Logs da aplicação
docker-compose -f docker-compose.dev.yml logs -f sra-dev

# Parar tudo
docker-compose -f docker-compose.dev.yml down

# Reset completo (limpa volumes)
docker-compose -f docker-compose.dev.yml down -v
```

### 1.5 Configuração do `.env`

Copie `.env.example` para `.env` e configure:

```bash
# LLM Providers (pelo menos um necessário)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...

# Scrapers (opcional, mas recomendado)
FIRECRAWL_API_KEY=fc-...
SPIDER_API_KEY=...
STEEL_API_KEY=...

# Modo de operação padrão
OPERATION_MODE=cirurgia

# Tracing (opcional)
JAEGER_ENDPOINT=http://localhost:4317
TRACING_ENABLED=true
TRACING_SAMPLER_RATIO=1.0

# Budget (opcional)
BUDGET_TOKENS_PER_QUERY=10000
BUDGET_COST_PER_QUERY_USD=5.0
BUDGET_TIMEOUT_SECONDS=300
```

### 1.6 Verificação do Setup

```bash
# Teste de conectividade
python -c "from src.orchestrator import Orchestrator; print('OK')"

# Health check dos serviços
curl http://localhost:3458/health
curl http://localhost:3024/api/v1/heartbeat  # ChromaDB
curl http://localhost:9090/-/healthy          # Prometheus

# Teste rápido de pesquisa
python cli/main.py search "test" --mode guerrilha
```

---

## 2. Arquitetura do Projeto

### 2.1 Estrutura de Diretórios

```
smart-research-agent/
├── src/
│   ├── pipeline/
│   │   ├── stages/           # Stages do pipeline (score_stage, etc.)
│   │   ├── fallback_manager.py
│   │   └── __init__.py
│   ├── search/
│   │   ├── api_searcher.py   # Classe intermediária para APIs
│   │   ├── base_searcher.py  # Interface abstrata
│   │   ├── factory.py        # Factory de searchers
│   │   ├── semantic_reranker.py
│   │   └── [searchers]/      # Implementações concretas
│   ├── services/
│   │   ├── search_service.py
│   │   ├── reasoning_service.py
│   │   ├── report_service.py
│   │   └── memory_service.py
│   ├── monitoring/
│   │   ├── health_monitor.py
│   │   ├── tracing.py        # OpenTelemetry
│   │   └── metrics.py
│   ├── ranking/
│   │   └── learned_ranker.py # Ranker ML
│   ├── utils/
│   │   ├── circuit_breaker.py
│   │   ├── rate_limiter.py
│   │   ├── retry.py
│   │   └── http_client.py
│   ├── cache/
│   │   └── cache.py
│   ├── types.py              # Dataclasses (SearchResult, RankedResult, etc.)
│   ├── config.py             # Configuração Pydantic
│   ├── operation_modes.py    # Modos de operação
│   └── orchestrator.py       # Facade principal
├── tests/
│   ├── integration/          # Testes de integração
│   │   └── test_pipeline.py
│   ├── benchmark/            # Benchmarks de performance
│   │   └── test_latency.py
│   ├── e2e/                  # Testes end-to-end
│   └── conftest.py           # Fixtures pytest
├── docs/
│   └── DEVELOPMENT.md        # Este arquivo
├── docker-compose.dev.yml
├── docker-compose.yml
├── pyproject.toml
├── README.md
└── .env
```

### 2.2 Pipeline de Pesquisa (9 Passos)

```
1. Análise de Intenção        → IntentAnalyzer
2. Expansão de Queries        → QueryExpander
3. Plano de Fontes            → SourcePlanner
4. Execução de Buscas         → SearchService + FallbackManager
5. Ranqueamento               → LearnedRanker / QualityRanker
6. Scoring de Confiança       → ScoreStage (novo)
7. Detecção de Gaps           → GapDetector
8. Síntese                    → Synthesizer
9. Geração de Relatório       → ReportGenerator
```

### 2.3 Componentes Novos (v6.0)

| Componente | Arquivo | Propósito |
|------------|---------|-----------|
| `ScoreStage` | `src/pipeline/stages/score_stage.py` | Scoring independente com anti-hallucination |
| `APISearcher` | `src/search/api_searcher.py` | Classe intermediária para searchers HTTP |
| `CircuitBreaker` (v2) | `src/utils/circuit_breaker.py` | CB com métricas, backoff, registry |
| `FallbackManager` | `src/pipeline/fallback_manager.py` | Gerenciamento centralizado de fallbacks |
| `TracingManager` | `src/monitoring/tracing.py` | OpenTelemetry com correlation IDs |
| `LearnedRanker` | `src/ranking/learned_ranker.py` | Ranker ML com LightGBM/XGBoost |

---

## 3. Como Adicionar um Novo Searcher

### 3.1 Escolha a Classe Base

**Para APIs HTTP:** Herde de `APISearcher` (recomendado)
**Para fontes não-HTTP:** Herde de `BaseSearcher`

### 3.2 Implementação com APISearcher (Recomendado)

```python
# src/search/gitlab_searcher.py
"""Searcher para GitLab API."""

from __future__ import annotations

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.types import SearchResult
from src.utils.circuit_breaker import CircuitBreakerConfig


class GitLabSearcher(APISearcher):
    """Busca repositórios e projetos no GitLab."""

    def __init__(self, config: dict):
        super().__init__(
            APISearcherConfig(
                source_name="gitlab",
                base_url="https://gitlab.com/api/v4",
                timeout=config.get("timeout", 30),
                max_results=config.get("max_results", 20),
                circuit_config=CircuitBreakerConfig(
                    name="gitlab_api",
                    failure_threshold=3,
                    recovery_timeout=300,
                ),
                cache_ttl=3600,
                auth_header="PRIVATE-TOKEN",
                auth_token=config.get("gitlab_token"),
                default_headers={"Accept": "application/json"},
            )
        )

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Busca projetos no GitLab."""
        params = {
            "search": query,
            "per_page": self.max_results,
            "order_by": "last_activity_at",
        }
        data = await self._make_request("GET", "/projects", params=params)
        return [self.normalize(item) for item in data]

    def normalize(self, raw: dict) -> SearchResult:
        """Normaliza resposta da API GitLab."""
        return SearchResult(
            source="gitlab",
            title=raw.get("name_with_namespace", "Unknown"),
            url=raw.get("web_url", ""),
            description=raw.get("description", ""),
            metrics={
                "stars": raw.get("star_count", 0),
                "forks": raw.get("forks_count", 0),
                "language": raw.get("default_branch", ""),
                "updated_at": raw.get("last_activity_at"),
            },
            raw=raw,
        )
```

### 3.3 Registro no Factory

```python
# src/search/factory.py

from src.search.gitlab_searcher import GitLabSearcher

class SearcherFactory:
    @staticmethod
    def create_searchers(orchestrator):
        searchers = {}
        # ... searchers existentes ...

        # GitLab (se token disponível)
        if config.gitlab_token:
            searchers["gitlab"] = GitLabSearcher(config.__dict__)

        return searchers
```

### 3.4 Adição ao Modo de Operação

```python
# src/operation_modes.py

"concorrencia": OperationConfig(
    name="concorrencia",
    searchers=[
        "producthunt",
        "hackernews",
        "reddit",
        "github",
        "gitlab",  # <-- ADICIONADO
        "google",
        "brave",
    ],
    # ... resto da config ...
)
```

### 3.5 Testes do Novo Searcher

```python
# tests/search/test_gitlab_searcher.py

import pytest
from unittest.mock import AsyncMock, patch
from src.search.gitlab_searcher import GitLabSearcher


@pytest.mark.asyncio
async def test_gitlab_search_basic():
    searcher = GitLabSearcher({"gitlab_token": "test-token"})

    with patch.object(searcher, "_make_request", new_callable=AsyncMock) as mock:
        mock.return_value = [
            {
                "name_with_namespace": "gitlab-org / gitlab",
                "web_url": "https://gitlab.com/gitlab-org/gitlab",
                "description": "GitLab CE",
                "star_count": 1000,
                "forks_count": 500,
                "last_activity_at": "2026-06-01T00:00:00Z",
            }
        ]

        results = await searcher.search("gitlab")

        assert len(results) == 1
        assert results[0].source == "gitlab"
        assert results[0].metrics["stars"] == 1000
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_gitlab_normalization():
    searcher = GitLabSearcher({"gitlab_token": "test"})
    raw = {
        "name_with_namespace": "user / project",
        "web_url": "https://gitlab.com/user/project",
        "description": "A project",
        "star_count": 42,
    }
    result = searcher.normalize(raw)
    assert result.title == "user / project"
    assert result.metrics["stars"] == 42
```

### 3.6 Checklist de Novo Searcher

- [ ] Implementa `search()` e `normalize()`
- [ ] Usa `APISearcher` (se HTTP) ou `BaseSearcher` (se não-HTTP)
- [ ] Configura `CircuitBreaker` via `APISearcherConfig`
- [ ] Registra no `SearcherFactory`
- [ ] Adiciona a pelo menos um `OperationMode`
- [ ] Escreve testes unitários
- [ ] Adiciona ao `FallbackManager` (se houver alternativas)
- [ ] Documenta no `README.md` (se público)

---

## 4. Como Adicionar um Novo Stage

### 4.1 Conceito de Stage

Um **Stage** é uma unidade de processamento do pipeline com:
- Entrada e saída bem definidas
- Sem estado compartilhado (stateless)
- Possibilidade de skip (para dados estruturados)
- Integração com tracing

### 4.2 Implementação de um Stage

```python
# src/pipeline/stages/validation_stage.py
"""Stage de validação de resultados antes do ranking."""

from __future__ import annotations

import logging
from typing import Any

from src.pipeline.stages.score_stage import PipelineStage
from src.types import RankedResult

logger = logging.getLogger("pipeline.validation_stage")


class ValidationStage:
    """Valida resultados de busca antes do ranqueamento.

    Responsabilidades:
      - Remover resultados com URLs malformadas
      - Filtrar resultados com descrição vazia
      - Verificar duplicatas por URL
    """

    async def execute(
        self,
        results: list[RankedResult],
        context: dict[str, Any] | None = None,
    ) -> list[RankedResult]:
        """Executa validação e retorna resultados filtrados."""
        if not results:
            return []

        valid = []
        seen_urls = set()

        for r in results:
            # Skip duplicatas
            if r.url in seen_urls:
                continue
            seen_urls.add(r.url)

            # Skip sem URL
            if not r.url or not r.url.startswith("http"):
                continue

            # Skip sem descrição
            if not r.description or len(r.description.strip()) < 20:
                continue

            valid.append(r)

        logger.info(f"ValidationStage: {len(results)} → {len(valid)} resultados")
        return valid
```

### 4.3 Integração no Orchestrator

```python
# src/orchestrator.py

from src.pipeline.stages.validation_stage import ValidationStage

class Orchestrator:
    def __init__(self, config: Config = None):
        # ... inicializações existentes ...
        self.validation_stage = ValidationStage()

    async def _execute_searches(self, query, intent, expanded_queries, source_plan):
        # ... buscas ...
        ranked = await self.ranker.rank(results, query=query)

        # NOVO: Stage de validação
        ranked = await self.validation_stage.execute(ranked, context={"query": query})

        # Stage de scoring (existente)
        scored = await self.score_stage.execute(ranked, context={"query": query})

        return scored
```

### 4.4 Stage com Tracing

```python
# Instrumentando o execute do Stage para tracing
from src.monitoring.tracing import trace_pipeline_stage

class ValidationStage:
    @trace_pipeline_stage("validation")
    async def execute(self, results, context=None):
        # ... implementação ...
        return valid
```

### 4.5 Checklist de Novo Stage

- [ ] Implementa `execute(data, context)` com contrato claro
- [ ] É stateless (sem estado entre chamadas)
- [ ] Retorna mesmo tipo de entrada (transformação)
- [ ] Adiciona tracing com `@trace_pipeline_stage`
- [ ] Registra métricas (input/output count, latency)
- [ ] Documenta no docstring o propósito e contrato
- [ ] Escreve testes unitários
- [ ] Integra no Orchestrator na ordem correta

---

## 5. Como Rodar Testes

### 5.1 Estrutura de Testes

```
tests/
├── conftest.py              # Fixtures globais
├── test_*.py                # Testes unitários
├── integration/
│   └── test_pipeline.py     # Testes de integração
├── benchmark/
│   └── test_latency.py      # Benchmarks de performance
└── e2e/
    └── test_full_pipeline.py # Testes end-to-end
```

### 5.2 Comandos de Execução

```bash
# Todos os testes
pytest

# Com cobertura
pytest --cov=src --cov-report=html --cov-report=term

# Testes específicos
pytest tests/test_bloco3_resiliencia.py -v
pytest tests/integration/test_pipeline.py -v
pytest tests/benchmark/test_latency.py -v

# Apenas testes de circuit breaker
pytest -k "circuit" -v

# Apenas benchmarks
pytest -k "benchmark" --benchmark-only

# Com logs detalhados
pytest -v -s --log-cli-level=INFO

# Paralelo (4 workers)
pytest -n 4

# Falha no primeiro erro
pytest -x

# Re-executa apenas falhas
pytest --lf
```

### 5.3 Testes de Integração

```bash
# Pipeline completo com mocks
pytest tests/integration/test_pipeline.py::TestPipelineWithMockedSearchers -v

# Fallbacks
pytest tests/integration/test_pipeline.py::TestFallbacks -v

# Circuit breakers
pytest tests/integration/test_pipeline.py::TestCircuitBreakers -v

# Budget enforcement
pytest tests/integration/test_pipeline.py::TestBudgetEnforcement -v

# Benchmark de latência por modo
pytest tests/integration/test_pipeline.py::TestLatencyBenchmark -v
```

### 5.4 Benchmarks de Performance

```bash
# Latência por searcher
pytest tests/benchmark/test_latency.py::TestSearcherLatencyPercentiles -v

# Identificação de searchers lentos
pytest tests/benchmark/test_latency.py::TestSlowSearcherDetection -v

# Paralelismo com semáforo
pytest tests/benchmark/test_latency.py::TestParallelSearchWithSemaphore -v

# Early termination
pytest tests/benchmark/test_latency.py::TestEarlyTermination -v

# Exportar resultados JSON
pytest tests/benchmark/test_latency.py --benchmark-json=results.json
```

### 5.5 Testes E2E

```bash
# Requer API keys reais no .env
pytest tests/e2e/test_full_pipeline.py -v

# Modo mock (sem APIs externas)
pytest tests/e2e/test_full_pipeline.py -v --mock
```

### 5.6 Configuração do pytest

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-ra -q --strict-markers"
markers = [
    "slow: marks tests as slow (deselect with '-m not slow')",
    "benchmark: marks tests as benchmarks",
    "integration: marks tests as integration tests",
    "e2e: marks tests as end-to-end tests",
    "stress: marks tests as stress tests",
]
```

---

## 6. Como Medir Custo e Latência

### 6.1 Métricas de Latência

#### 6.1.1 Por Searcher (Benchmark)

```bash
# Executa benchmark e exporta JSON
pytest tests/benchmark/test_latency.py --benchmark-json=latency_report.json

# Saída esperada:
# {
#   "github": {"p50": 120, "p95": 180, "p99": 250, "error_rate": 0.0},
#   "reddit": {"p50": 250, "p95": 380, "p99": 450, "error_rate": 0.05},
#   ...
# }
```

#### 6.1.2 Por Modo de Operação

```python
# Script de medição
from src.operation_modes import OperationModes
from src.orchestrator import Orchestrator
import time

modes = ["guerrilha", "cirurgia", "radar", "black_ops"]
results = {}

for mode_name in modes:
    mode = OperationModes.get_mode(mode_name)
    orch = Orchestrator(config)
    orch.apply_mode(mode)

    start = time.monotonic()
    report = await orch.research("test query")
    elapsed = time.monotonic() - start

    results[mode_name] = {
        "elapsed_seconds": elapsed,
        "timeout_configured": mode.timeout_seconds,
        "searchers_used": len(mode.searchers),
    }
```

#### 6.1.3 Por Stage do Pipeline

```python
from src.monitoring.tracing import TracingManager, TracingConfig

# Inicializa tracing
tracing = TracingManager(TracingConfig(
    service_name="sra-dev",
    backend=ExportBackend.CONSOLE,  # Para dev
))
tracing.init()

# Cada stage é automaticamente instrumentado
# Spans aparecem no console com latência
```

### 6.2 Métricas de Custo

#### 6.2.1 Budget Tracker

```python
from src.monitoring.budget_tracker import BudgetTracker

# Configura limites
tracker = BudgetTracker(
    max_tokens_per_query=10000,
    max_cost_usd=5.0,
)

# Registra cada chamada LLM
tracker.record_call(
    model="claude-sonnet-4",
    input_tokens=1500,
    output_tokens=800,
    cost_usd=0.045,
)

# Verifica status
status = tracker.get_status()
# {
#   "tokens_used": 2300,
#   "tokens_remaining": 7700,
#   "cost_usd": 0.045,
#   "cost_remaining_usd": 4.955,
#   "within_budget": True,
# }
```

#### 6.2.2 Estimativa de Custo por Modo

| Modo | Searchers | LLM Calls | Est. Tokens | Est. Custo USD |
|------|-----------|-----------|-------------|----------------|
| Guerrilha | 5 | 3 | ~3K | ~$0.15 |
| Cirurgia | 8 | 8 | ~12K | ~$0.60 |
| Radar | 5 | 3 | ~3K | ~$0.15 |
| Black Ops | 12 | 12 | ~20K | ~$1.00 |
| Debate | 8 | 15 | ~25K | ~$1.25 |

#### 6.2.3 Dashboard de Custo

```bash
# Prometheus metrics expostas em http://localhost:8001/metrics
# Métricas relevantes:
# - sra_search_latency_seconds{searcher="github"}
# - sra_llm_tokens_total{model="claude"}
# - sra_llm_cost_usd_total
# - sra_fallback_usage_total{stage="search"}
# - sra_circuit_breaker_state{breaker="github_api"}
```

### 6.3 Otimização de Custo

```python
# 1. Use cache agressivo
config.cache_strategy = "aggressive"

# 2. Use modo guerrilha para queries simples
config.operation_mode = "guerrilha"

# 3. Limite max_depth
config.max_depth = 1

# 4. Desabilite auditoria
config.enable_auditor = False

# 5. Use sampling de tracing em produção
TracingConfig(sampler_ratio=0.1)  # 10% dos requests
```

### 6.4 Alertas de Budget

```python
# No Orchestrator
if tracker.tokens_used > tracker.max_tokens * 0.8:
    logger.warning("Budget: 80% dos tokens consumidos")

if tracker.cost_usd > tracker.max_cost_usd * 0.9:
    logger.error("Budget: 90% do custo atingido — reduzindo profundidade")
    config.max_depth = max(1, config.max_depth - 1)
```

---

## 7. Referência Rápida

### 7.1 Comandos Úteis

```bash
# Desenvolvimento
uvicorn api.main:app --port 3458 --reload
streamlit run ui/streamlit_app.py
python cli/main.py search "query" --mode cirurgia

# Docker
docker-compose -f docker-compose.dev.yml up -d
docker-compose -f docker-compose.dev.yml logs -f sra-dev
docker-compose -f docker-compose.dev.yml down -v

# Testes
pytest -x                          # Falha rápida
pytest --lf                        # Re-executa falhas
pytest -k "not slow"               # Ignora testes lentos
pytest --benchmark-json=out.json   # Exporta benchmarks

# Debug
python -m src.orchestrator         # Teste rápido
python -c "from src.config import Config; print(Config())"
```

### 7.2 Variáveis de Ambiente

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `OPERATION_MODE` | `cirurgia` | Modo de operação padrão |
| `ANTHROPIC_API_KEY` | — | Chave da API Anthropic |
| `OPENAI_API_KEY` | — | Chave da API OpenAI |
| `FIRECRAWL_API_KEY` | — | Chave do Firecrawl |
| `REDIS_URL` | `redis://localhost:6379` | URL do Redis |
| `CHROMADB_HOST` | `localhost` | Host do ChromaDB |
| `NEO4J_URI` | `bolt://localhost:7687` | URI do Neo4j |
| `JAEGER_ENDPOINT` | — | Endpoint OTLP do Jaeger |
| `TRACING_ENABLED` | `false` | Habilita tracing |
| `BUDGET_TOKENS_PER_QUERY` | `10000` | Máximo de tokens por query |
| `BUDGET_COST_USD` | `5.0` | Máximo de custo USD por query |

### 7.3 Links Úteis

- **API Docs:** http://localhost:3458/docs
- **Swagger UI:** http://localhost:3458/redoc
- **Prometheus:** http://localhost:9090
- **Grafana:** http://localhost:3000 (admin/admin)
- **Jaeger UI:** http://localhost:16686
- **ChromaDB:** http://localhost:3024

### 7.4 Troubleshooting

```bash
# Problema: "ModuleNotFoundError: src"
# Solução: Instale em modo editable
pip install -e ".[dev]"

# Problema: "Connection refused" ao Redis
# Solução: Verifique se Redis está rodando
docker-compose -f docker-compose.dev.yml ps redis
redis-cli ping  # deve retornar PONG

# Problema: Testes falham com "Event loop is closed"
# Solução: Use pytest-asyncio correto
pytest --asyncio-mode=auto

# Problema: Latência alta no Firecrawl
# Solução: Verifique rate limit ou use fallback
# curl http://localhost:3458/health/firecrawl

# Problema: Circuit breaker sempre aberto
# Solução: Reset manual ou verifique health check
python -c "from src.utils.circuit_breaker import get_default_registry;            import asyncio;            r = asyncio.run(get_default_registry());            asyncio.run(r.reset_all())"
```

---

## Contribuindo

1. Fork o repositório
2. Crie uma branch: `git checkout -b feat/nova-funcionalidade`
3. Implemente com testes
4. Execute a suíte completa: `pytest`
5. Abra um PR com descrição clara

---

*Documento mantido pela equipe SRA. Para dúvidas, abra uma issue no GitHub.*
