# MISSÃO CLAUDE — Fase 3 Grupos C/D/E: Notícias, Jurídico e Marketplaces

**Projeto:** `E:\Meus LLMs\smart-research-agent`
**Pré-requisito:** Fases 0+1+2+3A+3B já concluídas e em `main`. Execute `git pull origin main` antes de começar.

---

## CONTEXTO

Esta missão implementa os searchers de maior diferencial competitivo do SRA: notícias em tempo real, jurídico/governo/patentes e marketplaces/produtos.

---

## LEIA ANTES DE TUDO

1. `E:\Meus LLMs\Conversa\PLANO_SRA_BUSCA_UNIVERSAL.md` — Seção "Fase 3 Grupos C/D/E"
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

## REGRA DE OURO (igual às fases anteriores)

1. Herdar de `APISearcher` ou `ScrapingSearcher` — nunca de `BaseSearcher` diretamente
2. Usar `@register_searcher` de `src/search/registry.py`
3. Fontes de scraping → adicionar em `UNTRUSTED_SOURCES`
4. Cada searcher → seu próprio arquivo de teste com mocks

---

## GRUPO C — NOTÍCIAS E TEMPO REAL

### TAREFA C.1 — `NewsAPISearcher`
**Arquivo:** `src/search/newsapi_searcher.py`

Herda de `APISearcher`. Usa a NewsAPI.org (https://newsapi.org/docs/endpoints/everything).

**Funcionalidades:**
- Endpoint: `GET https://newsapi.org/v2/everything?q={query}&sortBy=relevancy&pageSize=10&apiKey={key}`
- Chave via env `NEWSAPI_KEY` — se não configurada, retorna `[]` sem erro (graceful)
- Parse de `articles[]` com campos `title`, `url`, `description`, `source.name`, `publishedAt`
- `source_name="newsapi"`
- Registrado via `@register_searcher("newsapi", requires_key="NEWSAPI_KEY")`

**Teste:** `tests/test_newsapi_searcher.py`

---

## GRUPO D — JURÍDICO, GOVERNO E PATENTES

### TAREFA D.1 — `CourtListenerSearcher`
**Arquivo:** `src/search/courtlistener_searcher.py`

Herda de `APISearcher`. Usa a API pública do CourtListener (https://www.courtlistener.com/api/rest/v4/).

**Funcionalidades:**
- Endpoint: `GET https://www.courtlistener.com/api/rest/v4/search/?q={query}&type=o&format=json`
- Parse de `results[]` com campos `caseName`, `absoluteUrl` (prefixar com `https://www.courtlistener.com`), `snippet`
- `source_name="courtlistener"`
- Registrado via `@register_searcher("courtlistener", enabled_env="SRA_COURTLISTENER_ENABLED")`
- Header: `Authorization: Token {COURTLISTENER_API_TOKEN}` se env presente, caso contrário usa sem auth (API pública tem limite mais baixo)

**Teste:** `tests/test_courtlistener_searcher.py`

---

### TAREFA D.2 — `GooglePatentsSearcher`
**Arquivo:** `src/search/googlepatents_searcher.py`

Herda de `ScrapingSearcher`. Usa cascata Firecrawl→Spider→Jina para raspar resultados de `https://patents.google.com/`.

**Funcionalidades:**
- URL de busca: `https://patents.google.com/xhr/query?url=q%3D{query_encoded}&exp=&download=false`
- Alternativa: usar a API pública do Google Patents via SerpAPI se `SERPAPI_KEY` disponível
- Parse retorna: `patent_id`, `title`, `abstract`, `url`, `assignee`, `filing_date`
- `source_name="google_patents"`
- Registrado via `@register_searcher("google_patents", enabled_env="SRA_PATENTS_ENABLED")`

**Teste:** `tests/test_googlepatents_searcher.py`

---

### TAREFA D.3 — `SECEdgarSearcher`
**Arquivo:** `src/search/sec_edgar_searcher.py`

Herda de `APISearcher`. Usa a API pública EDGAR da SEC (https://efts.sec.gov/LATEST/search-index?q=).

**Funcionalidades:**
- Endpoint: `GET https://efts.sec.gov/LATEST/search-index?q={query}&dateRange=custom&startdt=2020-01-01&forms=10-K,8-K`
- Parse de `hits.hits[]` com campos `_source.display_names`, `_source.file_date`, `_source.period_of_report`, `_id` (form URL)
- `source_name="sec_edgar"`
- Registrado via `@register_searcher("sec_edgar", enabled_env="SRA_EDGAR_ENABLED")`
- Header: `User-Agent: smart-research-agent info@example.com` (obrigatório pela SEC)

**Teste:** `tests/test_sec_edgar_searcher.py`

---

## GRUPO E — MARKETPLACES E APPS

### TAREFA E.1 — `MercadoLivreSearcher`
**Arquivo:** `src/search/mercadolivre_searcher.py`

Herda de `APISearcher`. Usa a API pública do MercadoLivre (https://api.mercadolibre.com/sites/MLB/search).

**Funcionalidades:**
- Endpoint: `GET https://api.mercadolibre.com/sites/MLB/search?q={query}&limit=10`
- Parse de `results[]` com campos `title`, `permalink`, `price`, `currency_id`, `condition`, `thumbnail`
- Formata `description` como: `"R$ {price} ({condition}) — {seller_address.city}"`
- `source_name="mercadolivre"`
- Registrado via `@register_searcher("mercadolivre", enabled_env="SRA_MERCADOLIVRE_ENABLED")`

**Teste:** `tests/test_mercadolivre_searcher.py`

---

### TAREFA E.2 — `AppStoreSearcher`
**Arquivo:** `src/search/appstore_searcher.py`

Herda de `APISearcher`. Usa a iTunes Search API (sem chave).

**Funcionalidades:**
- Endpoint: `GET https://itunes.apple.com/search?term={query}&entity=software&limit=10&country=br`
- Parse de `results[]` com campos `trackName`, `trackViewUrl`, `description`, `averageUserRating`, `primaryGenreName`, `price`
- `source_name="appstore"`
- Registrado via `@register_searcher("appstore", enabled_env="SRA_APPSTORE_ENABLED")`

**Teste:** `tests/test_appstore_searcher.py`

---

### TAREFA E.3 — Atualizar `UNTRUSTED_SOURCES`
**Arquivo:** `src/pipeline/stages/search_stage.py`

Adicione ao conjunto:
```python
"google_patents",   # conteúdo raspado de páginas de patentes
"mercadolivre",     # descrições de vendedores (texto livre)
```

---

## TAREFA FINAL — Rodar Todos os Testes do Grupo C/D/E
```bash
pytest tests/test_newsapi_searcher.py tests/test_courtlistener_searcher.py tests/test_googlepatents_searcher.py tests/test_sec_edgar_searcher.py tests/test_mercadolivre_searcher.py tests/test_appstore_searcher.py -v
```

---

## COMMIT FINAL

```bash
git add .
git commit --no-verify -m "feat: Fase 3 Grupos C/D/E — NewsAPI, CourtListener, Patents, SEC Edgar, MercadoLivre, AppStore"
```

---

## STATUS ESPERADO AO FINALIZAR

| Searcher | Herança | Key Obrigatória | Teste |
|----------|---------|-----------------|-------|
| `NewsAPISearcher` | `APISearcher` | `NEWSAPI_KEY` (graceful sem key) | ✅ |
| `CourtListenerSearcher` | `APISearcher` | Opcional | ✅ |
| `GooglePatentsSearcher` | `ScrapingSearcher` | Não | ✅ |
| `SECEdgarSearcher` | `APISearcher` | Não | ✅ |
| `MercadoLivreSearcher` | `APISearcher` | Não | ✅ |
| `AppStoreSearcher` | `APISearcher` | Não | ✅ |
| Commit na branch `main` | — | — | ✅ |
