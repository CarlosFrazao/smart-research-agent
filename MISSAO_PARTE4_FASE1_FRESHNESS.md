# MISSÃO PARTE4 — FASE 1: Corrigir o Mecanismo de Freshness (published_at ausente)

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 1 do Plano Parte 4 do Smart Research Agent.
> Pré-requisito: **Plano Parte 3 (todas as fases 1-6) concluído**.
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — POR QUE ESTA FASE É CRÍTICA E BLOQUEANTE

O mecanismo de `freshness` do `HybridRanker` (`src/ranking/hybrid_ranker.py`) foi desenhado com meia-vida por fonte (RSS=3 dias, Reddit=7, etc.), mas **nunca funciona de verdade** porque `SearchResult` não tem o campo `published_at`. O único campo existente é `fetched_at` com `default_factory=datetime.now`, que fica sempre perto de zero (o momento da busca). Resultado: **para qualquer busca ao vivo, todos os resultados parecem igualmente frescos**, independente de terem sido publicados há 10 minutos ou há 3 dias.

**Para o objetivo do Plano Parte 4 (notícias em tempo real), isso é bloqueante**: sem `published_at` funcionando, nenhuma das fontes de notícia da Fase 2 consegue priorizar o mais recente. Esta fase é pré-requisito direto para as Fases 2 e 3.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | Alterações em types.py, hybrid_ranker.py e searchers |
| `test-driven-development` | `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md` | Escrever testes de freshness |
| `clean-code` | `E:\Meus LLMs\.claude\skills\clean-code\SKILL.md` | Garantir backward compat e fallbacks |

---

## 📋 TAREFAS (em ordem obrigatória)

### TAREFA 1.1 — Adicionar campo `published_at` em `SearchResult`

**Arquivo:** `src/types.py`

```python
# Dentro de SearchResult, logo após fetched_at (por volta da linha 260):
published_at: datetime | None = None
# Data/hora de publicação original do conteúdo, conforme reportada pela fonte.
# None = fonte não expõe timestamp de publicação (fallback: fetched_at será usado).
```

Garantir que o campo seja serializável e compatível com as classes que herdam de `SearchResult`.

**Validação:**
```bash
python -m py_compile src/types.py
python -m pytest tests/test_part1_foundation.py -v
```

---

### TAREFA 1.2 — Corrigir `_compute_freshness()` no HybridRanker

**Arquivo:** `src/ranking/hybrid_ranker.py` (por volta da linha 582)

Atenção: A assinatura existente do método é `def _compute_freshness(self, result: SearchResult, now: datetime) -> float`. **NÃO altere a assinatura para não quebrar quem o chama na linha 566.** 

Ajuste o método para suportar `published_at` com tratamento de timezone robusto para evitar erros de `TypeError: can't subtract offset-naive and offset-aware datetimes`:

```python
    def _compute_freshness(self, result: SearchResult, now: datetime) -> float:
        """Computa score de freshness (0-1) baseado na idade do resultado."""
        published_at = getattr(result, "published_at", None)
        reference_time = published_at or getattr(result, "fetched_at", None)
        if reference_time is None:
            return 0.5

        try:
            if isinstance(reference_time, str):
                # Caso venha serializado como string do JSON
                reference_time = datetime.fromisoformat(reference_time.replace("Z", "+00:00"))
            
            # Normalizar comparação para evitar erro de naive vs aware datetime
            if reference_time.tzinfo is not None:
                now_cmp = now
            else:
                now_cmp = datetime.now()
                
            age_days = (now_cmp - reference_time).total_seconds() / 86400.0
        except Exception:
            return 0.5

        halflife = FRESHNESS_HALFLIFE.get(result.source, FRESHNESS_HALFLIFE["default"])
        score = math.exp(-age_days / halflife)

        if age_days <= self.config.freshness_boost_days:
            score = min(1.0, score * 1.3)

        return score
```

---

### TAREFA 1.3 — Adicionar meia-vidas para fontes de notícia no HybridRanker

**Arquivo:** `src/ranking/hybrid_ranker.py`

Localizar o dicionário `FRESHNESS_HALFLIFE` (por volta da linha 56) e adicionar entradas para as novas fontes de notícia da Fase 2 (valores em fração de dias):

```python
FRESHNESS_HALFLIFE: Dict[str, float] = {
    # entradas existentes...
    "github": 90.0,
    "reddit": 7.0,
    "hackernews": 14.0,
    "arxiv": 365.0,
    "stackoverflow": 180.0,
    "rss": 3.0,
    # Novas entradas para fontes de notícia:
    "gdelt": 0.5,            # 12 horas — notícia de minuto a minuto
    "google_news_rss": 0.5,  # 12 horas
    "newsapi_org": 0.5,      # 12 horas
    "bluesky": 0.25,         # 6 horas — rede social decai super rápido
    "mastodon_social": 0.25, # 6 horas
    "default": 30.0,
}
```

---

### TAREFA 1.4 — Popular `published_at` nos searchers existentes

**Arquivos:**
- `src/search/rss_searcher.py` — popular usando a data convertida do feedparser.
- `src/search/hackernews_searcher.py` — converter o timestamp do campo `time`.
- `src/search/reddit_searcher.py` — converter `created_utc` para `datetime`.

Garantir que a conversão resulte em um objeto `datetime` válido (preferencialmente UTC aware para consistência).

---

### TAREFA 1.5 — Testes unitários de Freshness

**Arquivo a criar:** `tests/test_freshness_published_at.py`

Escrever testes unitários cobrindo:
1. `_compute_freshness` usando `fetched_at` quando `published_at` is None.
2. `_compute_freshness` usando `published_at` quando disponível.
3. Tratamento defensivo de datetime naive vs aware (não gera crash).
4. Resultados muito recentes recebendo boost e decay correto com base na halflife da fonte.

```bash
python -m pytest tests/test_freshness_published_at.py -v
```

---

### TAREFA 1.6 — Commit

```bash
git add src/types.py src/ranking/hybrid_ranker.py src/search/rss_searcher.py \
        src/search/hackernews_searcher.py src/search/reddit_searcher.py \
        tests/test_freshness_published_at.py
git commit -m "feat(parte4/fase1): adiciona published_at em SearchResult e corrige freshness no HybridRanker"
git push origin main
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 1

- [ ] `SearchResult.published_at` adicionado em `src/types.py`
- [ ] `_compute_freshness()` adaptado mantendo a assinatura original e protegendo contra crash de timezone
- [ ] `FRESHNESS_HALFLIFE` com novas meias-vidas de notícias
- [ ] Searchers de RSS, Reddit e HN populam `published_at`
- [ ] Testes verdes em `tests/test_freshness_published_at.py`
- [ ] Sem quebras na suíte de testes existente (`python -m pytest tests/test_hybrid_ranker.py`)
