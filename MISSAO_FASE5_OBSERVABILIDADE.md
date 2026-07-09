# MISSÃO CLAUDE — Fase 5: Observabilidade, Custo e Performance em Escala

**Projeto:** `E:\Meus LLMs\smart-research-agent`
**Pré-requisito:** Fases 0+1+2+3+4 já concluídas e em `main`. Execute `git pull origin main` antes de começar.

---

## CONTEXTO

Com 20+ fontes simultâneas, o SRA precisa de observabilidade fina para evitar que uma única fonte lenta ou cara degrade toda a experiência. Esta fase adiciona:
1. Dashboard de custo por fonte/sessão
2. SLAs de timeout diferenciados por categoria de fonte
3. Benchmark de deduplicação com alto volume
4. Limpeza de referências residuais a Neo4j (dívida técnica 0.5)

---

## LEIA ANTES DE TUDO

1. `E:\Meus LLMs\Conversa\PLANO_SRA_BUSCA_UNIVERSAL.md` — Seção "Fase 5" e "0.5"
2. `E:\Meus LLMs\CLAUDE.md` — Governança, skills e protocolo de boot
3. Leia os arquivos antes de modificar:
   - `src/utils/token_economy.py`
   - `src/utils/budget_tracker.py`
   - `api/main.py` (endpoint `/api/circuit-breakers`)
   - `src/pipeline/stages/search_stage.py`

---

## SKILLS OBRIGATÓRIAS

Carregue ANTES de escrever qualquer código:
- `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md`
- `E:\Meus LLMs\.claude\skills\clean-code\SKILL.md`
- `E:\Meus LLMs\.claude\skills\api-patterns\SKILL.md`

---

## TAREFA 1 — Custo por fonte no `BudgetTracker`
**Arquivo:** `src/utils/budget_tracker.py`

Adicione rastreamento por fonte:

```python
def record_source_cost(
    self,
    source_name: str,
    session_id: str,
    tokens_used: int = 0,
    requests_made: int = 1,
    latency_ms: float = 0.0,
) -> None:
    """Registra custo de uma chamada a uma fonte específica."""
    ...

def get_source_cost_summary(self, session_id: str) -> dict[str, dict]:
    """Retorna custo acumulado por fonte na sessão.
    
    Retorna: {
        "github": {"requests": 5, "tokens": 200, "avg_latency_ms": 120},
        "firecrawl": {"requests": 3, "tokens": 800, "avg_latency_ms": 2100},
        ...
    }
    """
    ...
```

---

## TAREFA 2 — Endpoint de custo por fonte na API
**Arquivo:** `api/main.py`

Adicione o endpoint:

```python
@router.get("/api/source-costs/{session_id}")
async def get_source_costs(session_id: str):
    """Retorna custo e performance por fonte de busca para uma sessão."""
    summary = budget_tracker.get_source_cost_summary(session_id)
    return {
        "session_id": session_id,
        "sources": summary,
        "total_requests": sum(s["requests"] for s in summary.values()),
        "slowest_source": max(summary, key=lambda k: summary[k]["avg_latency_ms"], default=None),
    }
```

---

## TAREFA 3 — SLAs de timeout diferenciados por categoria de fonte
**Arquivo:** `src/pipeline/stages/search_stage.py`

Substitua o timeout único por timeouts diferenciados por categoria:

```python
# Timeouts por categoria (em segundos)
SOURCE_TIMEOUT_MAP: dict[str, float] = {
    # APIs estruturadas rápidas
    "github": 8.0,
    "arxiv": 8.0,
    "hackernews": 6.0,
    "wikipedia": 5.0,
    "duckduckgo": 5.0,
    "npm": 5.0,
    "pypi": 5.0,
    "cratesio": 5.0,
    "appstore": 5.0,
    "newsapi": 8.0,
    "courtlistener": 10.0,
    "sec_edgar": 12.0,
    # Agregadores e fontes médias
    "reddit": 10.0,
    "producthunt": 10.0,
    "rss": 8.0,
    "mercadolivre": 10.0,
    # Scraping/agentes — timeout maior
    "firecrawl": 30.0,
    "spider": 25.0,
    "steel": 25.0,
    "quora": 20.0,
    "google_patents": 20.0,
    "discourse": 15.0,
    # Default para fontes não mapeadas
    "_default_api": 10.0,
    "_default_scraping": 25.0,
}

def get_timeout_for_source(source_name: str) -> float:
    """Retorna timeout em segundos para uma fonte específica."""
    if source_name in SOURCE_TIMEOUT_MAP:
        return SOURCE_TIMEOUT_MAP[source_name]
    if source_name in UNTRUSTED_SOURCES:
        return SOURCE_TIMEOUT_MAP["_default_scraping"]
    return SOURCE_TIMEOUT_MAP["_default_api"]
```

Use `get_timeout_for_source(source_name)` em vez do timeout fixo no `_search_one_source()`.

---

## TAREFA 4 — Limpeza de referências a Neo4j (Fase 0.5)
**Tarefa de auditoria e documentação, não de remoção funcional.**

Faça uma auditoria nos seguintes arquivos e adicione um comentário `# LEGACY: Neo4j mantido como backend opcional via Docker profile 'neo4j'` em cada referência encontrada, para que futuros desenvolvedores saibam que é intencional (não um esquecimento):

- `config.py` — procure por `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`
- `graph_explorer_agent.py` — procure por `neo4j`
- `src/pipeline/stages/graph_explorer_stage.py` — procure por `neo4j`
- `src/memory/knowledge_graph.py` — procure por `neo4j`

Adicione também uma seção no `CHANGELOG.md` ou `README.md`:
```markdown
### Backend Neo4j (Legado Opcional)
O backend Neo4j foi substituído pelo KuzuDB (v6.2.0) como padrão.
As referências ao Neo4j mantidas no código são intencionais e habilitadas
via Docker Compose profile `neo4j` para usuários que precisam de compatibilidade.
Para ativar: `docker-compose --profile neo4j up`
```

---

## TAREFA 5 — Testes de benchmark (deduplicação com alto volume)
**Arquivo:** `tests/benchmark/test_dedup_high_volume.py`

```python
import pytest
import time

@pytest.mark.benchmark
def test_conflict_detection_with_20_sources():
    """Garante que conflict_detector não regride em performance com 20+ fontes."""
    from src.conflict_detector import ConflictDetector
    
    # Simular 200 resultados de 20 fontes diferentes
    results = [
        {"url": f"https://source{i % 20}.com/result{j}", "title": f"Result {j}", "score": 0.7}
        for i in range(20) for j in range(10)
    ]
    
    detector = ConflictDetector()
    start = time.monotonic()
    deduped = detector.deduplicate(results)
    elapsed = time.monotonic() - start
    
    assert elapsed < 2.0, f"Deduplicação lenta: {elapsed:.2f}s para 200 resultados"
    assert len(deduped) <= len(results)

@pytest.mark.benchmark
def test_search_stage_timeout_per_source():
    """Verifica que timeouts são diferenciados por categoria de fonte."""
    from src.pipeline.stages.search_stage import get_timeout_for_source, UNTRUSTED_SOURCES
    
    assert get_timeout_for_source("github") < get_timeout_for_source("firecrawl")
    assert get_timeout_for_source("wikipedia") <= 10.0
    assert get_timeout_for_source("quora") >= 15.0
```

---

## COMMIT FINAL

```bash
git add .
git commit --no-verify -m "feat: Fase 5 — observabilidade de custo por fonte, SLAs de timeout e limpeza Neo4j"
```

---

## STATUS ESPERADO AO FINALIZAR

| Critério | Esperado |
|----------|----------|
| `BudgetTracker.record_source_cost()` implementado | ✅ |
| Endpoint `/api/source-costs/{session_id}` funcionando | ✅ |
| `SOURCE_TIMEOUT_MAP` no `search_stage.py` | ✅ |
| Timeouts diferenciados por categoria aplicados | ✅ |
| Referências Neo4j comentadas/documentadas | ✅ |
| Benchmarks de deduplicação passando | ✅ |
| Commit na branch `main` | ✅ |
