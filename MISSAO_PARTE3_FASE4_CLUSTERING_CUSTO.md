# MISSÃO PARTE3 — FASE 4: Clustering de Resultados + Estimativa de Custo

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 4 do plano derivado de `PLANO_SRA_PARTE_3.md`.
> Pré-requisito: **Fases 1, 2 e 3 concluídas** (`SearchResult` com `cluster_id`/`corroborated_by`).
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — POR QUE CLUSTERING VEM ANTES DA UI

Sem clustering, adicionar mais fontes ao SRA só aumenta o ruído redundante no topo do ranking — a mesma notícia de 5 fontes aparece como 5 itens separados competindo por espaço. O clustering é o mecanismo que faz "mais fontes = mais confiança", não "mais fontes = mais bagunça".

**Decisão de design crítica:** O clustering **reaproveitará os vetores já calculados pelo `HybridRanker`** — não criará um segundo modelo de embedding. Dois modelos diferentes no mesmo pipeline gerariam distâncias não-comparáveis. O custo incremental é praticamente zero.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | Todo código novo |
| `test-driven-development` | `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md` | Testes de clustering e estimativa de custo |
| `clean-code` | `E:\Meus LLMs\.claude\skills\clean-code\SKILL.md` | Integração cirúrgica no rank_stage existente |

---

## 📋 TAREFAS (em ordem obrigatória)

### TAREFA 4.1 — Implementar `cluster_similar_results()` em `rank_stage.py`

**Contexto (§9, §10.1-§10.3):** Roda **depois do `RankStage`** (que já produziu `RankedResult` com embedding) e **antes do `ScoreStage`/`SynthesizeStage`**. Não precisa virar um `PipelineStage` novo — é uma função a mais no estágio que já roda.

**O que fazer:**

1. Abrir `src/pipeline/stages/rank_stage.py` e `src/ranking/hybrid_ranker.py`.
2. Confirmar onde os embeddings são calculados e em que formato estão disponíveis no contexto após o ranking.
3. Adicionar a função de clustering dentro de `rank_stage.py`:

```python
import numpy as np
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.types import RankedResult  # ajuste o import conforme o projeto real


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Similaridade de cosseno entre dois vetores numpy."""
    norm_a, norm_b = np.linalg.norm(a), np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def cluster_similar_results(
    ranked: list,           # list[RankedResult] — tipagem completa no código real
    embeddings: dict,       # {url: np.ndarray} — reaproveitado do HybridRanker
    similarity_threshold: float = 0.88,  # configurável via env var CLUSTER_THRESHOLD
) -> None:
    """Agrupa resultados de FONTES DIFERENTES sobre o mesmo fato/evento.

    Modifica ranked[i].result.cluster_id e .corroborated_by in-place.
    Threshold conservador: falso positivo (fundir resultados diferentes) é pior
    que falso negativo (deixar de fundir resultados iguais).

    REGRAS:
    - Nunca clusteriza resultados da MESMA fonte — resultados de uma fonte
      são provavelmente genuinamente distintos, não corroboração.
    - threshold=0.88 é ponto de partida teórico — validar com dados reais
      e ajustar via CLUSTER_SIMILARITY_THRESHOLD env var.
    """
    import os
    threshold = float(os.environ.get("CLUSTER_SIMILARITY_THRESHOLD", similarity_threshold))

    for i, r_i in enumerate(ranked):
        result_i = getattr(r_i, "result", r_i)  # adaptar ao tipo real
        if result_i.cluster_id is not None:
            continue  # já clusterizado
        url_i = getattr(result_i, "url", "")
        if url_i not in embeddings:
            continue

        group = [i]
        for j in range(i + 1, len(ranked)):
            r_j = ranked[j]
            result_j = getattr(r_j, "result", r_j)
            if result_j.cluster_id is not None:
                continue
            if result_j.source == result_i.source:
                continue  # não clusteriza dentro da mesma fonte
            url_j = getattr(result_j, "url", "")
            if url_j not in embeddings:
                continue
            sim = _cosine_similarity(embeddings[url_i], embeddings[url_j])
            if sim >= threshold:
                group.append(j)

        if len(group) > 1:
            cid = f"cluster_{i}"
            sources_in_group = [getattr(getattr(ranked[k], "result", ranked[k]), "source", "") for k in group]
            for k in group:
                result_k = getattr(ranked[k], "result", ranked[k])
                result_k.cluster_id = cid
                result_k.corroborated_by = [
                    s for s in sources_in_group if s != result_k.source
                ]
```

4. Chamar `cluster_similar_results()` dentro do método `run()` do `RankStage`, **após o ranking** e usando os embeddings já calculados pelo `HybridRanker`:

```python
# No método run() do RankStage, após o ranking:
if hasattr(context, "embeddings") and context.embeddings:
    cluster_similar_results(context.ranked_results, context.embeddings)
    logger.debug(
        "Clustering: %d resultados agrupados em clusters",
        sum(1 for r in context.ranked_results
            if getattr(getattr(r, "result", r), "cluster_id", None) is not None)
    )
```

**Validação:**
```bash
python -m py_compile src/pipeline/stages/rank_stage.py
```

---

### TAREFA 4.2 — Atualizar `ScoreStage` para usar `max()` por cluster

**Contexto (§10.3):** O score do cluster deve ser o `max()` dos scores individuais — um resultado excelente + um mediano corroborando ainda é um resultado excelente, corroborado.

**O que fazer:**

1. Abrir `src/pipeline/stages/score_stage.py` (ou onde o scoring acontece).
2. Adicionar lógica para propagar o `max_score` do cluster para todos os membros:

```python
# Após o scoring individual, dentro de ScoreStage.run():
# Propagar max_score do cluster para todos os membros
cluster_scores: dict[str, float] = {}
for r in context.ranked_results:
    result = getattr(r, "result", r)
    cid = getattr(result, "cluster_id", None)
    if cid:
        score = getattr(r, "combined_score", getattr(result, "confidence_score", 0.0))
        cluster_scores[cid] = max(cluster_scores.get(cid, 0.0), score)

for r in context.ranked_results:
    result = getattr(r, "result", r)
    cid = getattr(result, "cluster_id", None)
    if cid and cid in cluster_scores:
        # Usar max do cluster, não score individual
        if hasattr(r, "combined_score"):
            r.combined_score = cluster_scores[cid]
```

---

### TAREFA 4.3 — Atualizar `ReportStage` para renderizar clusters com "confirmado por N fontes"

**Contexto (§10.3):** O relatório deve mostrar "confirmado por N fontes: reddit, hn, productHunt" em vez de N itens separados sobre o mesmo evento.

**O que fazer:**

1. Abrir `src/pipeline/stages/report_stage.py` e localizar onde os resultados são formatados/listados.
2. Agrupar por `cluster_id` antes de renderizar, exibindo o melhor resultado do cluster como representante:

```python
# Antes de renderizar resultados no relatório:
from itertools import groupby

# Separar clusterizados dos não-clusterizados
clustered: dict[str, list] = {}
unclustered: list = []

for r in context.ranked_results:
    result = getattr(r, "result", r)
    cid = getattr(result, "cluster_id", None)
    if cid:
        clustered.setdefault(cid, []).append(r)
    else:
        unclustered.append(r)

# Para cada cluster, usar o resultado com maior score como representante
cluster_reps = []
for cid, members in clustered.items():
    rep = max(members, key=lambda x: getattr(x, "combined_score", 0.0))
    cluster_reps.append(rep)

final_results = cluster_reps + unclustered
# Ordenar pelo score
final_results.sort(key=lambda x: getattr(x, "combined_score", 0.0), reverse=True)

# Ao renderizar o representante do cluster, mostrar corroboração:
for r in final_results:
    result = getattr(r, "result", r)
    corroborated_by = getattr(result, "corroborated_by", [])
    if corroborated_by:
        # Ex: "Confirmado por: reddit, hackernews"
        corroboration_note = f"✅ Confirmado por: {', '.join(corroborated_by)}"
        # Incluir no template de relatório
```

---

### TAREFA 4.4 — Implementar estimativa de custo pré-busca

**Contexto (§8):** O `token_economy.py` já mede custo histórico por fonte. Falta agregar uma estimativa **antes** de executar a busca.

**O que fazer:**

1. Abrir `src/token_economy.py` e confirmar quais métodos expõem custo histórico por fonte.
2. Em `expand_stage.py` ou `source_planner.py`, após montar o `SourcePlan`, calcular:

```python
def estimate_search_cost(source_plan, token_economy, n_queries: int = 1) -> float:
    """Estima custo total em USD para executar o SourcePlan.

    Fórmula: Σ (custo_médio_histórico[fonte] × n_queries) por fonte no plano.
    Retorna 0.0 se não houver histórico suficiente para estimar.
    """
    total = 0.0
    all_sources = list(getattr(source_plan, "primary", [])) + list(getattr(source_plan, "secondary", []))
    for source_id in all_sources:
        avg_cost = getattr(token_economy, "get_avg_cost_per_source", lambda s: 0.0)(source_id)
        total += avg_cost * n_queries
    return total
```

3. Expor a estimativa no contexto antes da execução:

```python
context.extra["estimated_cost_usd"] = estimate_search_cost(
    source_plan=context.source_plan,
    token_economy=self.token_economy,
    n_queries=len(context.expanded_queries or [1]),
)
logger.info("Custo estimado da busca: ~$%.4f", context.extra["estimated_cost_usd"])
```

4. Adicionar endpoint `?dry_run=true` em `src/mcp_server.py` que retorna a estimativa sem executar:

```python
@app.post("/research")
async def research(request: ..., dry_run: bool = False):
    # ... montar context e source_plan normalmente ...
    if dry_run:
        cost = estimate_search_cost(source_plan, token_economy)
        return {"estimated_cost_usd": cost, "sources": list(source_plan.primary)}
    # ... continuar execução normal ...
```

---

### TAREFA 4.5 — Testes do clustering e estimativa de custo

**Arquivo alvo:** `tests/test_clustering.py` + `tests/test_cost_estimation.py` (arquivos NOVOS)

```python
# tests/test_clustering.py
"""Testes do algoritmo de clustering de resultados."""
import numpy as np
import pytest
from unittest.mock import MagicMock

# Importar cluster_similar_results conforme o caminho real no projeto


class TestClusterSimilarResults:

    def _make_result(self, source: str, url: str):
        r = MagicMock()
        r.result.source = source
        r.result.url = url
        r.result.cluster_id = None
        r.result.corroborated_by = []
        r.combined_score = 0.5
        return r

    def _embeddings(self, urls: list, similar_pairs: list = None) -> dict:
        """Cria embeddings onde pares listados têm similaridade > 0.88."""
        embs = {}
        for url in urls:
            embs[url] = np.random.randn(384)
            embs[url] /= np.linalg.norm(embs[url])
        # Forçar similaridade alta entre pares especificados
        if similar_pairs:
            for u1, u2 in similar_pairs:
                embs[u2] = embs[u1] + np.random.randn(384) * 0.01
                embs[u2] /= np.linalg.norm(embs[u2])
        return embs

    def test_same_source_not_clustered(self):
        from src.pipeline.stages.rank_stage import cluster_similar_results
        r1 = self._make_result("reddit", "https://reddit.com/1")
        r2 = self._make_result("reddit", "https://reddit.com/2")
        embs = self._embeddings(["https://reddit.com/1", "https://reddit.com/2"],
                                similar_pairs=[("https://reddit.com/1", "https://reddit.com/2")])
        cluster_similar_results([r1, r2], embs)
        assert r1.result.cluster_id is None
        assert r2.result.cluster_id is None

    def test_different_sources_similar_content_clustered(self):
        from src.pipeline.stages.rank_stage import cluster_similar_results
        r1 = self._make_result("reddit", "https://reddit.com/news")
        r2 = self._make_result("hackernews", "https://hn.com/news")
        embs = self._embeddings(["https://reddit.com/news", "https://hn.com/news"],
                                similar_pairs=[("https://reddit.com/news", "https://hn.com/news")])
        cluster_similar_results([r1, r2], embs)
        assert r1.result.cluster_id == r2.result.cluster_id
        assert "hackernews" in r1.result.corroborated_by
        assert "reddit" in r2.result.corroborated_by
```

**Validação:**
```bash
python -m pytest tests/test_clustering.py -v
python -m pytest tests/test_cost_estimation.py -v
python -m pytest tests/ --tb=short -q
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 4

- [ ] `cluster_similar_results()` implementada e chamada no `RankStage`
- [ ] `ScoreStage` usa `max()` do cluster, não média individual
- [ ] `ReportStage` renderiza "confirmado por N fontes" para resultados clusterizados
- [ ] `estimate_search_cost()` implementada e exposta em `context.extra["estimated_cost_usd"]`
- [ ] Endpoint `?dry_run=true` retorna estimativa sem executar a busca
- [ ] `python -m pytest tests/test_clustering.py -v` → todos passam
- [ ] `python -m pytest tests/ --tb=short -q` → zero novas regressões
- [ ] Commit com todos os arquivos desta fase

---

## 🚫 FORA DO ESCOPO DESTA FASE

- Segundo modelo de embedding → usar só o `all-MiniLM-L6-v2` já existente
- Clustering dentro da mesma fonte → proibido por design
- UI de allowlist/denylist → Fase 5
- `GenericWebsiteSearcher` → decisão de produto pendente
- Paginação das fontes genéricas → limitação conhecida v1
