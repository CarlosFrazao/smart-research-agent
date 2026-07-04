# Migração dos searchers para APISearcher / ScrapingSearcher

## O que foi feito nesta entrega

Refatorei a camada `src/search/` para eliminar a duplicação de:
HTTP client, rate limiting, cache e circuit breaker — que antes cada
searcher reimplementava por conta própria.

Novo esqueleto:

```
src/search/
├── __init__.py
├── base_searcher.py          # BaseSearcher, APISearcher, ScrapingSearcher, SearchResult
├── common/
│   ├── __init__.py
│   ├── http_client.py        # SharedHTTPClient (singleton, pool único, retry/backoff)
│   ├── rate_limiter.py       # RateLimiter + RateLimiterRegistry (token bucket por fonte)
│   ├── cache.py               # CacheBackend/InMemoryTTLCache + CacheRegistry (TTL por fonte)
│   ├── circuit_breaker.py    # CircuitBreaker + CircuitBreakerRegistry (por fonte)
│   └── exceptions.py         # SearcherError e subclasses
├── github_searcher.py        # GithubSearcher(APISearcher)
├── reddit_searcher.py        # RedditSearcher(APISearcher)
├── hn_searcher.py             # HNSearcher(APISearcher)
├── arxiv_searcher.py          # ArxivSearcher(APISearcher)
├── producthunt_searcher.py   # ProductHuntSearcher(APISearcher)
├── awesome_searcher.py       # AwesomeListSearcher(ScrapingSearcher)
├── web_searcher.py           # WebSearcher(ScrapingSearcher)
└── firecrawl_searcher.py     # FirecrawlSearcher(ScrapingSearcher)
```

## Por que só 8 searchers, e não 18

Não consegui ler o conteúdo atual do repositório
`CarlosFrazao/smart-research-agent` além da raiz e do
`README_DE_IMPLEMENTACAO.md` (a API do GitHub, o raw.githubusercontent.com
e as páginas `/tree/` e `/blob/` individuais ficaram inacessíveis para mim
neste ambiente). Esse README documenta explicitamente 8 searchers +
`base_searcher.py`. Se o repositório real já cresceu para 18, os outros 10
não foram vistos por mim e **não foram inventados** — construí o padrão
correto e apliquei fielmente aos 8 que pude confirmar.

## Como aplicar o mesmo padrão aos searchers restantes

Para cada searcher que falta (ex: `youtube_searcher.py`,
`stackoverflow_searcher.py`, etc. — o que quer que os outros 10 sejam):

1. **Decida a classe-base:**
   - Resposta é JSON/XML de uma API formal (com contrato estável) →
     `APISearcher`.
   - HTML pensado para navegador, serviço de extração de conteúdo, ou
     scraping de texto livre (Markdown, etc.) → `ScrapingSearcher`.

2. **Delete** do arquivo atual: criação de `httpx.Client`/`requests.Session`,
   qualquer `time.sleep`/contador manual de rate limit, qualquer
   `dict`/`lru_cache` ad-hoc, e qualquer `try/except` genérico de
   "desistir depois de N falhas".

3. **Implemente apenas dois métodos:**
   ```python
   def build_request(self, query, **kwargs) -> tuple[url, params_ou_body, headers]: ...
   def parse_response(self, data, *, query, **kwargs) -> list[SearchResult]: ...
   # (ou parse_content, se ScrapingSearcher)
   ```

4. **Configure** `SearcherSettings` com o `cache_ttl_seconds`,
   `rate_limit` e `circuit_breaker` adequados ao SLA real da fonte (os
   valores em cada arquivo desta entrega são estimativas conservadoras —
   ajuste com os limites reais documentados por cada API/serviço).

5. Se o transporte não for `GET` simples (ex: GraphQL, POST com corpo
   JSON), sobrescreva `_fetch` como fiz em `producthunt_searcher.py` e
   `firecrawl_searcher.py` — o resto do template method (cache, rate
   limit, circuit breaker) continua funcionando sem alteração.

6. Registre a nova classe em `src/search/__init__.py`.

## Compatibilidade com o orquestrador existente

Se o orquestrador atual chama algo como `searcher.fetch(query)` ou
`searcher.run(query)` em vez de `searcher.search(query)`, adicione um alias
fino em `BaseSearcher` (ex: `fetch = search`) para não precisar tocar no
orquestrador nesta primeira fase — e migre o nome do método em um segundo
commit, separado do refactor estrutural.

## Testes

Não escrevi testes de integração reais (bateriam em APIs externas de
verdade). Recomendo:
- Testes unitários de `parse_response`/`parse_content` com fixtures JSON/HTML
  gravadas (sem rede).
- Um teste de `CircuitBreaker` simulando N falhas consecutivas e
  verificando a transição CLOSED → OPEN → HALF_OPEN → CLOSED.
- Um teste de `RateLimiter` verificando que `try_acquire()` recusa quando
  o bucket está vazio.

## O que eu preciso de você para fechar 100% fiel ao repo real

Cole aqui (ou suba como anexo) o `github_searcher.py` atual e mais 1-2
searchers dos outros 10 não documentados no README — com isso eu ajusto
nomes de métodos, assinaturas e comportamento específico (paginação,
autenticação, formato de erro) para bater exatamente com o que o
orquestrador já espera, em vez de uma reimplementação "genérica correta".
