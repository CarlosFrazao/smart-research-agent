# Smart Research Agent (SRA) v6.0 🚀

O **Smart Research Agent (SRA)** é um ecossistema autônomo de pesquisa inteligente projetado para obter, verificar, persistir e analisar informações técnicas a partir de múltiplas fontes distribuídas (GitHub, HN, Reddit, ArXiv, ProductHunt, Web scraping via Firecrawl e Jina) de forma assíncrona, robusta e resiliente contra bloqueios de rede.

---

## 📐 Arquitetura do Sistema

```mermaid
flowchart TD
    subgraph Client_Apps [Camada de Apresentação]
        A1[Streamlit Web UI - Port 8501] -->|HTTP REST| B[FastAPI REST Server - Port 3458]
        A2[Typer CLI / Console] -->|Chamada Direta ou REST| B
        A3[Swagger OpenAPI Docs - /docs] -->|Swagger UI| B
    end

    subgraph Service_API [Servidor FastAPI]
        B -->|Lifespan Setup| C1[Prometheus metrics HTTP Server - Port 8001]
        B -->|Lifespan Setup| C2[structlog JSON Configurator]
        B -->|Síncrono/Assíncrono| D[Orchestrator]
    end

    subgraph Background_Workers [Processamento em Segundo Plano]
        CeleryWorker[Celery + Redis Worker] -->|Executa Tarefa| D
    end

    subgraph Data_Storage [Persistência e Cache]
        D -->|Busca Híbrida RRF| VectorDB[ChromaDB Vector Store]
        D -->|Persistência Cypher| GraphDB[KuzuDB Embedded Graph]
        D -->|Smart Cache TTL| CacheDB[(Redis / Memória)]
    end
```

---

## 🛠️ Configuração e Instalação

### 1. Pré-requisitos
- Python 3.11 ou superior
- Docker & Docker Compose (para Redis, ChromaDB e serviços auxiliares)

### 2. Instalar Dependências Python
```powershell
pip install -r requirements.txt
# Ou instale manualmente as dependências principais:
pip install fastapi uvicorn streamlit typer rich structlog prometheus-client celery redis chromadb rank-bm25 cohere sentence-transformers kuzu
```

### 3. Subir Serviços Auxiliares (Redis, ChromaDB & Auxiliares)
```powershell
docker-compose up -d
```

### 4. Configurar Variáveis de Ambiente (`.env`)
Configure chaves de API, endereços de bancos de dados (Redis, ChromaDB, KuzuDB) e credenciais no arquivo `.env`.

> **Configuração de pesos e fontes (`config/`):** Os arquivos `config/scoring_weights.yaml` e `config/sources.yaml` são **documentação de referência de design** e **NÃO são lidos por nenhum código**. Os pesos reais do ranker híbrido vivem em constantes hardcoded em `src/ranking/hybrid_ranker.py` (`DEFAULT_BM25_WEIGHT`, etc., em `HybridRankerConfig`), e os searchers são instanciados pela `SearcherFactory` (`src/search/factory.py`). Para alterar pesos/fontes reais, edite esses módulos — não estes YAMLs. (Ver `MISSAO_PARTE2_FASE2_CONFIG_E_HITL.md`, Tarefa 2.1 — Opção B.)

> **Orquestrador padrão (v7.0+):** A partir da SRA v7.0, o **`ReActOrchestrator`** (loop dinâmico ReAct) é o orquestrador **padrão** — a flag `ENABLE_DYNAMIC_LOOP` tem `default=True`. Ele decide dinamicamente quais etapas do pipeline executar com base no contexto (confiança, lacunas, claims pendentes). Para reverter ao pipeline sequencial clássico (DAG fixo), defina `ENABLE_DYNAMIC_LOOP=false` no `.env`.

---

## 🚀 Como Executar

### 1. Servidor oficial (API REST + MCP)
O servidor oficial de produção é `src/mcp_server.py` — é o que o Dockerfile
sobe e o que expõe tanto as tools MCP quanto as rotas REST. Inicie-o com:
```powershell
uvicorn src.mcp_server:app --port 3458 --reload
```
Acesse a documentação OpenAPI em: [http://localhost:3458/docs](http://localhost:3458/docs)

As rotas REST de pesquisa/agendamento/observabilidade herdadas do módulo
legado `api/main.py` ficam disponíveis sob o prefixo `/api/v2`
(ex.: `POST /api/v2/api/research`), com autenticação `X-API-Key`, CORS por
env (`CORS_ALLOWED_ORIGINS`) e rate limiting por IP aplicados.

> **Legado:** `uvicorn api.main:app --port 3458 --reload` ainda funciona para
> uso standalone da API REST, mas prefira o servidor oficial acima.

### 2. Web UI Streamlit
Inicie a interface interativa no seu navegador:
```powershell
streamlit run ui/streamlit_app.py
```
Acesse em: [http://localhost:8501](http://localhost:8501)

### 3. Linha de Comando (CLI Typer)
Execute pesquisas completas de forma ergonômica direto no terminal:
```powershell
# Pesquisa direta
python cli/main.py search "Rust async best practices" --mode guerrilha

# Pesquisa salvando em Markdown
python cli/main.py search "Kubernetes trends 2026" -m cirurgia -o kubernetes.md

# Consultar status dos Circuit Breakers
python cli/main.py status

# Agendar uma pesquisa recorrente com alertas de mudança (webhook Slack/Discord)
python cli/main.py schedule "novidades em RAG" --cron "0 8 * * *" --webhook "https://hooks.slack.com/..."

# Listar / executar / cancelar pesquisas agendadas
python cli/main.py schedule-list
python cli/main.py schedule-run <job_id>
python cli/main.py schedule-cancel <job_id>
```

> As mesmas operações estão disponíveis via API REST: `POST /api/schedule`,
> `GET /api/schedule` e `DELETE /api/schedule/{job_id}`.

### 4. Celery Worker (Fila Assíncrona)
Inicie o processador de tarefas em segundo plano:
```powershell
celery -A src.worker.celery_app worker --loglevel=info
```

---

## 📡 Observabilidade e Telemetria

- **Métricas Prometheus:** Expostas dinamicamente na porta `8001` no endpoint `/metrics`. Acesse: [http://localhost:8001/metrics](http://localhost:8001/metrics).
- **Logs Estruturados:** Configurados via `structlog` com renderização JSON nativa em produção, otimizada para ingestão automática em Datadog, Grafana Loki, e AWS CloudWatch.

---

## 🧩 Adicionando novas fontes via YAML

O SRA é um **canivete suíço universal**: adicionar uma nova fonte de busca é
uma questão de **YAML, não de código Python**. O `GenericAPISearcher`
(`src/search/generic_api_searcher.py`) lê o catálogo
`config/generic_sources.yaml` em runtime e transforma qualquer API REST pública
em uma fonte de busca — ela é registrada automaticamente no `SearcherFactory` e
validada pelo teste de wiring.

### Exemplo — adicionar uma nova fonte

Basta acrescentar uma entrada em `config/generic_sources.yaml`:

```yaml
sources:
  - id: "openalex"                              # vira o nome do searcher
    name: "OpenAlex (Scholarly Works)"
    base_url: "https://api.openalex.org/works"
    query_param: "search"                       # ?search=<query>. Use null p/ query na URL
    result_path: "results"                      # JMESPath da lista de resultados
    title_field: "title"                        # campo do título (aceita aninhado: bibjson.title)
    url_template: "{id}"                         # {campo} é substituído por item["campo"]
    snippet_field: "doi"                         # campo da descrição/snippet
    max_results: 10
    timeout: 20
    headers:                                     # opcional
      Authorization: "Bearer {OPENALEX_API_KEY}" # {ENV_VAR} resolvido de os.environ
    extra_params:                                # opcional — params fixos
      mailto: "you@example.com"
```

**Campos:**

| Campo | Obrigatório | Descrição |
|---|---|---|
| `id` | ✅ | Identificador único (vira o nome do searcher) |
| `base_url` | ✅ | Endpoint da API. Pode conter `{query}` quando `query_param: null` |
| `result_path` | ✅ | Expressão JMESPath da lista de resultados. `null` = resposta é lista raiz |
| `query_param` | — | Nome do parâmetro de query (ex: `q`). `null` = query interpolada na URL |
| `title_field` / `snippet_field` | — | Campos JMESPath (suportam caminho aninhado, ex: `bibjson.title`) |
| `url_template` | — | Template da URL do resultado; `{campo}` = `item["campo"]` |
| `max_results` / `timeout` | — | Defaults: 10 resultados, 15s |
| `headers` / `extra_params` | — | Headers com `{ENV_VAR}` e parâmetros fixos de query |

Depois, opcionalmente, referencie o `id` da fonte em `config/domains.yaml`
(listas `primary`/`secondary`) para incluí-la no roteamento de um domínio.
Nenhum código Python novo é necessário.

### Busca universal via MCP

A tool MCP `search_anything(query, hint_domain=None, max_results=10)` faz uma
busca multi-fonte cobrindo todas as fontes disponíveis (incluindo as genéricas),
sem precisar conhecer a taxonomia interna do SRA.

### Operadores de busca avançada

A query aceita operadores estilo Google, extraídos automaticamente e aplicados
pelas fontes que os suportam (SearXNG, DuckDuckGo):

```
site:reddit.com melhor teclado mecânico
filetype:pdf machine learning
intitle:python tutorial
```

---

## 🔒 Segurança em Produção

A API REST (`api/main.py`) expõe endpoints que consomem tokens de LLM e podem
acionar scraping pago. Por padrão (sem `SRA_API_KEY`), ela roda **sem
autenticação** e com CORS `*` — configuração voltada a desenvolvimento local.
Antes de expor em produção, aplique as três camadas abaixo.

### 1. Autenticação por API Key (`SRA_API_KEY`)

Gere uma chave aleatória e forte:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Defina no `.env`:

```dotenv
SRA_API_KEY=cole_a_chave_gerada_acima
```

Com `SRA_API_KEY` configurada, todos os endpoints de pesquisa
(`POST /api/research`, `/api/research/async`, `/api/research/stream`) exigem o
header:

```http
X-API-Key: <sua-chave>
```

Requisições sem a chave (ou com chave incorreta) recebem `401 Unauthorized`.
`/health` e `/docs` permanecem abertos. Se `SRA_API_KEY` estiver ausente, um
aviso é emitido no startup e a API roda sem auth (compatibilidade local).

### 2. Restrição de CORS (`CORS_ALLOWED_ORIGINS`)

Em produção, restrinja as origens permitidas às da sua UI real (separadas por
vírgula):

```dotenv
CORS_ALLOWED_ORIGINS=https://seu-app.com,https://app.exemplo.com
```

Deixar `CORS_ALLOWED_ORIGINS=*` (default) permite qualquer origem — aceitável
apenas em dev local.

### 3. Rate Limiting por IP

Os endpoints de pesquisa aplicam rate limiting de **10 requisições/minuto por
IP** (via `slowapi`). Exceder o limite retorna `429 Too Many Requests`. Ajuste
o valor em `api/main.py` (decorador `@limiter.limit(...)`) conforme o custo
esperado por requisição.

### 4. Proxy Reverso (recomendado)

Não exponha o `uvicorn` diretamente. Coloque um proxy reverso (nginx ou Caddy)
à frente para:

- Terminar TLS (HTTPS) e aplicar cabeçalhos de segurança (HSTS, CSP, etc.);
- Fazer autenticação/rate limiting adicionais na borda, se desejado;
- Limitar o bind (evite `0.0.0.0` exposto publicamente sem firewall).

> ⚠️ A configuração padrão (sem `SRA_API_KEY`, CORS `*`) é apenas para
> desenvolvimento local. Nunca a deixe assim em produção.
