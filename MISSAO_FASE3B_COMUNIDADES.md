# MISSÃO CLAUDE — Fase 3 Grupo B: Searchers de Comunidades e Redes Sociais

**Projeto:** `E:\Meus LLMs\smart-research-agent`
**Pré-requisito:** Fase 0+1+2+3A já concluídas e em `main`. Execute `git pull origin main` antes de começar.

> [!CAUTION]
> **SEGURANÇA CRÍTICA:** Estas fontes (Discourse, Quora, Twitter/X, Telegram) retornam texto livre não-estruturado — o vetor clássico de prompt injection. O `LLMSanitizer` JÁ FOI PLUGADO no `search_stage.py` na Fase 0.4. Certifique-se de que `UNTRUSTED_SOURCES` inclua todos os novos searchers desta fase ANTES de ativá-los.

---

## CONTEXTO

Com a fundação (Fase 0+1) e o roteamento universal (Fase 2) prontos, estamos adicionando fontes de comunidade e redes sociais para cobrir opiniões, discussões e insights humanos que APIs técnicas não capturam.

---

## LEIA ANTES DE TUDO

1. `E:\Meus LLMs\Conversa\PLANO_SRA_BUSCA_UNIVERSAL.md` — Seção "Fase 3 Grupo B"
2. `E:\Meus LLMs\CLAUDE.md` — Governança, skills e protocolo de boot
3. `E:\Meus LLMs\.claude\skills\http-request-mastery\SKILL.md`
4. `E:\Meus LLMs\.claude\skills\web-scraping-resilience\SKILL.md`
5. `E:\Meus LLMs\.claude\skills\security-hardening\SKILL.md`

---

## SKILLS OBRIGATÓRIAS

Carregue ANTES de escrever qualquer código:
- `E:\Meus LLMs\.claude\skills\http-request-mastery\SKILL.md`
- `E:\Meus LLMs\.claude\skills\web-scraping-resilience\SKILL.md`
- `E:\Meus LLMs\.claude\skills\security-hardening\SKILL.md`
- `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md`

---

## REGRA DE OURO DESTA FASE

Todo searcher novo DEVE:
1. Herdar de `APISearcher` (se tem API) ou `ScrapingSearcher` (se não tem)
2. Usar o decorator `@register_searcher` de `src/search/registry.py`
3. Ser adicionado em `UNTRUSTED_SOURCES` em `src/pipeline/stages/search_stage.py`
4. Ter testes unitários com mock HTTP

---

## TAREFA 1 — `DiscourseSearcher`
**Arquivo:** `src/search/discourse_searcher.py`

Herda de `APISearcher`. Usa a API pública JSON do Discourse (`/search.json`).

**Funcionalidades:**
- URL base configurável via `DISCOURSE_BASE_URL` ou via config dict
- Endpoint: `GET {base_url}/search.json?q={query}&page={page}`
- Parse de `posts` e `topics` do JSON de resposta
- Retorna lista de `SearchResult` com `title`, `url`, `description`, `source_name="discourse"`
- Registrado via `@register_searcher("discourse", enabled_env="SRA_DISCOURSE_ENABLED")`

**Mínimo viável:** suporte ao fórum `discuss.python.org` como padrão.

---

## TAREFA 2 — `QuoraSearcher`
**Arquivo:** `src/search/quora_searcher.py`

Herda de `ScrapingSearcher`. Quora não tem API pública — usa cascata Firecrawl→Spider→Jina via `ScrapingSearcher`.

**Funcionalidades:**
- URL de busca: `https://www.quora.com/search?q={query_encoded}`
- Parse do HTML retornado pelo scraper para extrair perguntas e snippets de resposta
- `source_name="quora"`
- Registrado via `@register_searcher("quora", enabled_env="SRA_QUORA_ENABLED")`
- Timeout máximo: 15s (scraping é mais lento)

> **Nota:** Se o scraping retornar HTML bloqueado (anti-bot 403/Captcha), retorna lista vazia sem lançar exceção — graceful degradation.

---

## TAREFA 3 — `NPMSearcher`
**Arquivo:** `src/search/npm_searcher.py`

Herda de `APISearcher`. API pública sem chave: `https://registry.npmjs.org/-/v1/search`.

**Funcionalidades:**
- Endpoint: `GET https://registry.npmjs.org/-/v1/search?text={query}&size=10`
- Parse de `objects[].package` com campos `name`, `description`, `links.npm`, `version`, `keywords`
- `source_name="npm"`
- Registrado via `@register_searcher("npm", enabled_env="SRA_NPM_ENABLED")`

---

## TAREFA 4 — `CratesIOSearcher`
**Arquivo:** `src/search/cratesio_searcher.py`

Herda de `APISearcher`. API pública sem chave: `https://crates.io/api/v1/crates`.

**Funcionalidades:**
- Endpoint: `GET https://crates.io/api/v1/crates?q={query}&per_page=10`
- Parse de `crates[]` com campos `name`, `description`, `homepage`/`repository`, `newest_version`
- Header obrigatório: `User-Agent: smart-research-agent/1.0`
- `source_name="cratesio"`
- Registrado via `@register_searcher("cratesio", enabled_env="SRA_CRATESIO_ENABLED")`

---

## TAREFA 5 — `GoogleTrendsSearcher`
**Arquivo:** `src/search/googletrends_searcher.py`

Herda de `APISearcher`. Usa a biblioteca `pytrends` (já listada como dependência opcional) ou a API não-oficial do Google Trends.

**Funcionalidades:**
- Retorna interesse relativo ao longo do tempo para a query (últimas 12 meses)
- Formata os dados em `SearchResult` com `title = "Google Trends: {query}"`, `description = "Popularidade: {peak_interest}% no período..."`, `url = "https://trends.google.com/..."`
- `source_name="google_trends"`
- Registrado via `@register_searcher("google_trends", enabled_env="SRA_GOOGLE_TRENDS_ENABLED")`
- Se `pytrends` não estiver instalado, retorna lista vazia sem erro

---

## TAREFA 6 — Atualizar `UNTRUSTED_SOURCES` em `search_stage.py`
**Arquivo:** `src/pipeline/stages/search_stage.py`

Adicione ao conjunto `UNTRUSTED_SOURCES`:
```python
UNTRUSTED_SOURCES = frozenset({
    "firecrawl", "scraping", "searxng", "web",
    "multilingual", "playwright", "spider", "steel",
    "duckduckgo", "quora", "twitter", "telegram",
    # Novos desta fase:
    "discourse",  # texto livre de fórum
    "google_trends",  # dados numéricos, mas origem externa
})
```

---

## TAREFA 7 — Testes unitários para cada searcher
Crie um arquivo de teste por searcher:
- `tests/test_discourse_searcher.py`
- `tests/test_quora_searcher.py`
- `tests/test_npm_searcher.py`
- `tests/test_cratesio_searcher.py`
- `tests/test_googletrends_searcher.py`

Cada arquivo deve ter:
1. `test_{name}_init()` — testa instanciação e configuração
2. `test_{name}_search_success()` — mock de resposta HTTP válida e parse correto
3. `test_{name}_search_empty()` — mock de resposta vazia ou erro, deve retornar `[]` sem exceção
4. `test_{name}_registered()` — verifica que o searcher está no registry

---

## TAREFA 8 — Validação Final
```bash
pytest tests/test_discourse_searcher.py tests/test_npm_searcher.py tests/test_cratesio_searcher.py tests/test_googletrends_searcher.py tests/test_quora_searcher.py -v
```
Todos os testes devem passar.

---

## COMMIT FINAL

```bash
git add .
git commit --no-verify -m "feat: Fase 3 Grupo B — Discourse, Quora, NPM, CratesIO, GoogleTrends searchers"
```

---

## STATUS ESPERADO AO FINALIZAR

| Searcher | Herança | Registrado | Teste |
|----------|---------|------------|-------|
| `DiscourseSearcher` | `APISearcher` | `@register_searcher("discourse")` | ✅ |
| `QuoraSearcher` | `ScrapingSearcher` | `@register_searcher("quora")` | ✅ |
| `NPMSearcher` | `APISearcher` | `@register_searcher("npm")` | ✅ |
| `CratesIOSearcher` | `APISearcher` | `@register_searcher("cratesio")` | ✅ |
| `GoogleTrendsSearcher` | `APISearcher` | `@register_searcher("google_trends")` | ✅ |
| `UNTRUSTED_SOURCES` atualizado | — | — | ✅ |
| Commit na branch `main` | — | — | ✅ |
