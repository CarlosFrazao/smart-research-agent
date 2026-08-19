# Plano: Fechar os 3 Gaps rumo a "Suprema" (ARES-V5.6)

> **✅ CONCLUÍDO (2026-07-14)** — Fases A, B e C executadas e validadas.
> Os 3 gaps estão fechados; registros em `Conversa/chat_log.md` e `Conversa/handoff.md`.

> **Escopo:** 3 fases cirúrgicas, uma por sessão, sem novas dependências.
> **Regra de ouro:** antes de codar cada fase, ler a `SKILL.md` correspondente (carga atômica).
> **Critério de pronto por fase:** `py_compile` limpo + `pytest` verde + varredura anti-TODO vazia.

---

## GAP 1 — Resiliência de Integração (Firecrawl 401 + cascata morta)

### Evidência no código (por que é real)
- `src/search/firecrawl_searcher.py:41-43` — em exceção chama `self.fallback(query)`.
- `src/search/base_searcher.py:211` — `fallback()` **retorna `[]`** (lista vazia, sem cascata).
- `src/clients/firecrawl_client.py:69-77` — `_with_retry` só retenta em `429/503/timeout`; um **401 (token inválido)** NÃO é retryable → relança na hora → `FirecrawlSearcher` morre sem alternativa.
- `report_super_stress_test.md` confirma: `FIRECRAWL_API_KEY` → `401 Unauthorized`; PubMed/Web ficam sem fallback web funcional.

### Solução cirúrgica
1. **`src/clients/firecrawl_client.py`**
   - Adicionar `_is_auth_error(exc)`: detecta `401` / `AuthenticationError` / `"unauthorized"` / `"api key"`.
   - Em `search()` (e `_with_retry`), ao detectar auth error → setar `self.auth_failed = True` + log `ERROR` único e claro ("Firecrawl token inválido — desativando e roteando para fallback"). Não retries infinitos.
2. **`src/search/firecrawl_searcher.py`**
   - Aceitar `web_fallback: BaseSearcher | None` no `__init__`.
   - Em `search()`, no bloco `finally`, se `getattr(self.client, "auth_failed", False)` e houver `web_fallback`, delegar `return await self._run_web_fallback(query)`.
3. **`src/search/factory.py`** (linha 134)
   - Sempre instanciar um `JinaSearcher` dedicado (zero-config, `r.jina.ai`) como `jina_fallback`.
   - `searchers["firecrawl"] = FirecrawlSearcher({**cfg, "web_fallback": jina_fallback})`.
   - Respeitar `host_mode` (já troca firecrawl→jina) e env `FIRECRAWL_AUTODISABLE_ON_401` (default `True`).
4. **`src/search/jina_searcher.py`** — já resiliente (retorna `fallback`/`[]` em erro); reutilizar sem alteração.

### Testes (TDD)
- `tests/test_firecrawl_resiliencia.py`:
  - mock `FirecrawlClient.search` lançando `FirecrawlAuthenticationError` → `FirecrawlSearcher.search` retorna resultados do Jina fallback (não `[]`).
  - mock retornando 401 → `client.auth_failed == True`.
  - smoke: `FirecrawlSearcher` com `web_fallback=None` continua retornando `[]` (comportamento antigo preservado).

### Skills a carregar (antes de codar)
`python-pro` + `http-request-mastery` + `web-scraping-resilience` + `test-driven-development`

### Critério de pronto
- PubMed/Web fallback web funciona mesmo com Firecrawl 401. `pytest tests/test_firecrawl_resiliencia.py -v` verde. `grep -rn "TODO" src/` vazio.

---

## GAP 2 — PubMed alcançável via CLI

### Evidência no código (por que é real)
- `src/search/factory.py:155-162` — `PubMedSearcher` **é** instanciado e registrado (`searchers["pubmed"]`); `get_available_searchers()` já lista `"pubmed"` (linha 403).
- `src/operation_modes.py:69-230` — NENHUM dos 7 `OperationConfig` inclui `"pubmed"` na lista `searchers`.
- `config/domains.yaml:1-59` — nenhum domínio lista `pubmed` em `primary`/`secondary`.
- Consequência: `SourcePlanner` nunca seleciona `pubmed` a partir de query CLI normal; só via harness direto (confirmado em `report_super_stress_test.md`).

### Solução cirúrgica
1. **`src/operation_modes.py`**
   - Adicionar preset **`"academico"`**: `searchers=["pubmed", "arxiv", "semantic_scholar", "web", "searxng"]`, `scrapers=["jina","firecrawl"]`, `confidence_threshold=0.80`, `max_depth=3`, `enable_auditor=True`, `cost_optimization=False`, `active_personas=["prism_scientist"]`, `enable_adversarial_pass=True`.
   - Estender `auto_select()` com palavras-chave biomédicas: `"pubmed", "medical", "clinical", "trial", "biomed", "médico", "clínico", "ensaios", "doi", "health"`.
   - Validar no `validate_operation_modes()` (já valida `searchers` não-vazio e ranges).
2. **`config/domains.yaml`**
   - Novo domínio **`biomed`**: `primary: [pubmed, arxiv, semantic_scholar]`, `secondary: [clinicaltrials, web, searxng, crossref]`, `fallback_enabled: true`.
   - Adicionar `pubmed` ao `secondary` de `ai_ml` e `general` (já têm `open_library`, `core_ac_uk`, `doaj`, `openalex` —PubMed encaixa).
3. **`src/source_planner.py`** (verificar, não modificar se já roteia por `domains.yaml`)
   - Confirmar que o planner honra `domains.yaml` + presets; se houver allowlist hardcoded de searchers, adicionar `pubmed`.

### Testes (TDD)
- `tests/test_pubmed_alcancavel.py`:
  - `OperationModes.get_mode("academico").searchers` contém `"pubmed"`.
  - `OperationModes.auto_select("ensaios clínicos covid")` == `"academico"`.
  - `validate_operation_modes()` não levanta com o novo preset.
  - Carregar `domains.yaml` e checar que `biomed.primary` contém `pubmed`.

### Skills a carregar (antes de codar)
`python-pro` + `test-driven-development` + `clean-code`

### Critério de pronto
- `python -m cli.main "ensaios clínicos sobre X" -m academico -o reports/saida.md` executa e o `SourcePlanner` inclui `pubmed` no plano (verificável em log/relatório). `pytest tests/test_pubmed_alcancavel.py -v` verde.

---

## GAP 3 — DeepResearcher JSON-safe (crash em não-JSON)

### Evidência no código (por que é real)
- `src/clients/llm_client.py:682-701` — `generate_structured()` faz `json.loads(response)` **sem try/except**. Se o LLM devolver texto/markdown/vazio → `json.JSONDecodeError: Expecting value: line 1 column 1` (erro exato citado no `report_super_stress_test.md`).
- `src/deep_researcher.py:543-549` — `_generate_hypotheses` chama `generate_structured`; o `except Exception` (linha 548) captura e cai em hipóteses fixas de fallback. O pipeline não quebra, mas **perde hipóteses reais silenciosamente** (degradação invisível em deep mode).
- `generate_structured` também não tenta extrair JSON de dentro de markdown com ruído, nem repara/retenta.

### Solução cirúrgica
1. **`src/clients/llm_client.py` — `generate_structured()`**
   - Envolver `json.loads` em `try/except json.JSONDecodeError`.
   - Antes do parse: tentar extrair bloco JSON mesmo com ruído — regex para `\[.*\]` (array) ou `\{.*\}` (objeto), com `re.DOTALL`.
   - Em falha de parse: 1 retry com prompt de reparo ("sua resposta anterior não foi JSON válido..."), depois retornar `OutputValidationError`-controlada ou `{}`/`[]` conforme `schema["type"]`. Nunca estourar `JSONDecodeError` para o caller.
   - Tratar resposta vazia (`response.strip() == ""`) como falha de parse (vai ao retry/repair).
2. **`src/deep_researcher.py` — `_generate_hypotheses()`**
   - Capturar especificamente `OutputValidationError`/`json.JSONDecodeError` e, ao cair no fallback fixo, logar `WARNING` com a query (já faz, mas garantir que o fallback só ocorre após a tentativa de reparo do client).
   - Manter assinatura e comportamento de fallback existente (backward compatible).

### Testes (TDD)
- `tests/test_llm_client_generate_structured.py`:
  - mock `generate()` retornando markdown com ```json ... ``` → parse ok.
  - mock retornando texto solto com JSON no meio → regex extrai e parseia.
  - mock retornando lixo não-JSON → após retry, retorna `[]` (schema array) sem estourar.
  - mock retornando `""` → não estoura `JSONDecodeError`.
- `tests/test_deep_researcher_json_repair.py`:
  - `DeepResearcher._generate_hypotheses` com LLM retornando não-JSON → retorna lista (fallback) sem exceção; cobre o cenário do stress test.

### Skills a carregar (antes de codar)
`python-pro` + `test-driven-development` + `clean-code`

### Critério de pronto
- Deep mode não degrada silenciosamente nem estoura em JSON inválido. `pytest tests/test_llm_client_generate_structured.py tests/test_deep_researcher_json_repair.py -v` verde. Stress test `--mode deep` reprocessa sem `Expecting value`.

---

## Ordem de execução (uma fase por sessão)
1. **Fase A — GAP 1** (resiliência Firecrawl) → commit `fix(gap1): ...`
2. **Fase B — GAP 2** (PubMed via CLI) → commit `feat(gap2): ...`
3. **Fase C — GAP 3** (DeepResearcher JSON-safe) → commit `fix(gap3): ...`

Após as 3: rodar `pytest tests/ -q` sweep completo; atualizar `handoff.md` + `chat_log.md` (regra R20); marcar este plano como concluído.

## Bônus (fora dos 3 gaps, para o salto "suprema" — fases futuras)
- **Gap 4 — WIP/Experimental:** promover ou remover os módulos de `EXPERIMENTAL_MODULES.md` (criterios de promoção já definidos no arquivo).
- **Gap 5 — Benchmark de qualidade:** suite comparativa SRA vs. Perplexity/Gemini Deep Research em N queries âncora, medindo precisão/cobertura/custo.
