# MISSÃO PARTE4 — FASE 2: Fontes de Notícias Gerais (GDELT, Google News, NewsAPI, Bluesky, Mastodon)

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Pré-requisito: **Fase 1 concluída** (campo `published_at` + freshness corrigidos).
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — O GAP MAIS DIRETO PARA O OBJETIVO DO PLANO PARTE 4

Hoje o SRA não tem nenhuma fonte de jornalismo geral. O `RSSSearcher` só tem blogs de empresas de IA. Não há GDELT, NewsAPI, Google News, Bluesky nem Mastodon. **Esse é o gap número um** para transformar o SRA de uma ferramenta técnica em um canivete suíço de pesquisa geral.

Esta fase adiciona 5 novas fontes de notícia via `GenericAPISearcher` (YAML declarativo — sem código Python novo por fonte) e via um novo `GenericFeedSearcher` para fontes RSS.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | Criar GenericFeedSearcher, normalização de dados |
| `http-request-mastery` | `E:\Meus LLMs\.claude\skills\http-request-mastery\SKILL.md` | Integração com APIs REST e feeds RSS externos |
| `web-scraping-resilience` | `E:\Meus LLMs\.claude\skills\web-scraping-resilience\SKILL.md` | Tratar erros de rate limit, timeouts e feeds quebrados |
| `test-driven-development` | `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md` | Testes de conformidade com fixtures para cada fonte |

---

## 📋 TAREFAS (em ordem obrigatória)

### TAREFA 2.1 — Adicionar GDELT e NewsAPI no `generic_sources.yaml`

**Arquivo:** `config/generic_sources.yaml`

Adicionar as duas entradas JSON (adaptadas para YAML):

```yaml
  - id: "gdelt"
    enabled: true
    base_url: "https://api.gdeltproject.org/api/v2/doc/doc"
    query_param: "query"
    static_params:
      mode: "artlist"
      format: "json"
      maxrecords: "20"
      sort: "datedesc"
    result_path: "articles"
    mapping:
      title: "title"
      url_template: "{url}"
      description: "seendate"
      published_at: "seendate"   # popula published_at diretamente
      metrics:
        domain: "domain"
        language: "language"
        tone: "tone"
    requires_api_key: false
    source_timeout_seconds: 10

  - id: "newsapi_org"
    enabled: true
    base_url: "https://newsapi.org/v2/everything"
    query_param: "q"
    static_params:
      sortBy: "publishedAt"
      language: "pt"
    auth_type: "query_api_key"
    api_key_param: "apiKey"
    env_key: "NEWSAPI_KEY"
    result_path: "articles"
    mapping:
      title: "title"
      url_template: "{url}"
      description: "description"
      published_at: "publishedAt"   # popula published_at diretamente
      metrics:
        source_name: "source.name"
        published_at: "publishedAt"
    requires_api_key: true
    rate_limit_per_minute: 33
    source_timeout_seconds: 10
```

> **Nota:** O `GenericAPISearcher` já existe (Plano Parte 3, Fase 3). Esta tarefa só adiciona entradas no YAML — zero código Python.

---

### TAREFA 2.2 — Criar `GenericFeedSearcher` para fontes RSS (Google News + feeds gerais)

**Arquivo a criar:** `src/search/generic_feed_searcher.py`

O `GenericAPISearcher` usa `requests` + JSON path. Fontes RSS precisam de `feedparser`. Criar um searcher leve e declarativo que lê uma configuração YAML com a mesma estrutura do `GenericAPISearcher`, mas usa `feedparser` em vez de `requests` + JSON:

```python
class GenericFeedSearcher(BaseSearcher):
    """Searcher declarativo para fontes RSS/Atom configuradas em YAML.
    Usa feedparser — sem código Python por fonte.
    """
    def __init__(self, config: dict, ...):
        self.feed_url_template = config["base_url"]  # ex: "https://news.google.com/rss/search?q={query}&hl=pt-BR&gl=BR"
        self.mapping = config["mapping"]
        self.source_id = config["id"]
        ...

    async def search(self, query: str, ...) -> list[SearchResult]:
        url = self.feed_url_template.replace("{query}", urllib.parse.quote(query))
        feed = feedparser.parse(url)
        results = []
        for entry in feed.entries[:20]:
            result = SearchResult(
                title=entry.get(self.mapping["title"], ""),
                url=entry.get(self.mapping["url"], entry.get("link", "")),
                description=entry.get(self.mapping.get("description", "summary"), ""),
                source=self.source_id,
                published_at=self._parse_date(entry.get("published_parsed")),
                fetched_at=datetime.now(),
            )
            results.append(result)
        return results

    def _parse_date(self, parsed_tuple) -> datetime | None:
        if parsed_tuple:
            return datetime(*parsed_tuple[:6])
        return None
```

---

### TAREFA 2.3 — Adicionar Google News RSS na configuração de feeds

**Arquivo a criar/atualizar:** `config/generic_feeds.yaml` (novo arquivo, mesmo padrão do `generic_sources.yaml`)

```yaml
feeds:
  - id: "google_news_rss"
    enabled: true
    base_url: "https://news.google.com/rss/search?q={query}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    mapping:
      title: "title"
      url: "link"
      description: "summary"
      published_at: "published"
    requires_api_key: false
    source_timeout_seconds: 10
```

---

### TAREFA 2.4 — Adicionar Bluesky e Mastodon no `generic_sources.yaml`

```yaml
  - id: "bluesky"
    enabled: true
    base_url: "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
    query_param: "q"
    static_params:
      limit: "20"
      sort: "latest"
    result_path: "posts"
    mapping:
      title: "record.text"
      url_template: "https://bsky.app/profile/{author.handle}/post/{uri}"
      description: "record.text"
      published_at: "indexedAt"
      metrics:
        author: "author.displayName"
        likes: "likeCount"
    requires_api_key: false
    source_timeout_seconds: 8

  - id: "mastodon_social"
    enabled: true
    base_url: "https://mastodon.social/api/v2/search"
    query_param: "q"
    static_params:
      type: "statuses"
      limit: "20"
    result_path: "statuses"
    mapping:
      title: "content"
      url_template: "{url}"
      description: "content"
      published_at: "created_at"
      metrics:
        author: "account.username"
        favourites: "favourites_count"
    requires_api_key: false
    source_timeout_seconds: 8
```

---

### TAREFA 2.5 — Registrar novos searchers no `SearcherFactory`

**Arquivo:** `src/search/factory.py`

Registrar `GenericFeedSearcher` e as novas entradas YAML de forma que o `SourcePlanner` possa selecioná-las quando o domínio for `universal` ou `news`.

---

### TAREFA 2.6 — Popular `published_at` no `GenericAPISearcher` existente

**Arquivo:** `src/search/generic_api_searcher.py`

O `GenericAPISearcher` já popula `metrics` via JMESPath. Adicionar suporte para o campo especial `published_at` no mapeamento:

```python
# No método normalize() do GenericAPISearcher:
if "published_at" in self.mapping:
    raw_date = jmespath.search(self.mapping["published_at"], item)
    result.published_at = self._parse_date_flexible(raw_date)
```

---

### TAREFA 2.7 — Testes de conformidade com fixtures

**Arquivo a criar:** `tests/test_news_sources.py`

Cobrir:
1. `gdelt` retorna resultados com `published_at` populado.
2. `newsapi_org` retorna resultados com `published_at` populado (mockado com fixture JSON).
3. `google_news_rss` parseia feed RSS e popula `published_at`.
4. `bluesky` retorna resultados (fixture JSON mock).
5. Cada fonte retorna `source` correto no `SearchResult`.

---

### TAREFA 2.8 — Commit

```bash
git add config/generic_sources.yaml config/generic_feeds.yaml \
        src/search/generic_feed_searcher.py src/search/generic_api_searcher.py \
        src/search/factory.py tests/test_news_sources.py
git commit -m "feat(parte4/fase2): fontes de notícia — GDELT, Google News RSS, NewsAPI, Bluesky, Mastodon"
git push origin main
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 2

- [ ] GDELT e NewsAPI configurados em `generic_sources.yaml` com `published_at` mapeado
- [ ] `GenericFeedSearcher` criado em `src/search/generic_feed_searcher.py`
- [ ] Google News RSS configurado em `config/generic_feeds.yaml`
- [ ] Bluesky e Mastodon configurados em `generic_sources.yaml`
- [ ] Todos os 5 searchers registrados no `SearcherFactory`
- [ ] `GenericAPISearcher.normalize()` popula `result.published_at`
- [ ] `tests/test_news_sources.py` — todos os testes verdes
- [ ] `python -m pytest tests/ --tb=short -q` — zero novas regressões
- [ ] Commit e push realizados

---

## 🚫 FORA DO ESCOPO DESTA FASE

- Roteamento dinâmico (domínio `universal`) — será na Fase 5.
- Tool MCP `monitor_topic` — será na Fase 4.
- Multi-perspectiva via tom do GDELT (`tone`) — será na Fase 5.
