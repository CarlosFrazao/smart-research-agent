---
name: smart-research-agent
description: >
  Skill operacional para uso do Smart Research Agent v3.0 — motor local de pesquisa profunda
  com 18 tools MCP cobrindo GitHub, Reddit, HackerNews, ArXiv, ProductHunt, Awesome Lists,
  Web, Firecrawl, Deep Research, Confidence Scoring, Feedback Loop, Search Universal,
  Vigília de Tópicos, Trending GDELT e Smart Model Routing.
  Serviço Docker SSE na porta 3458 (montagem /mcp). Memória persistente via OrvixMemory (SQLite RRF).
  version: 3.1
---

# 🔎 SMART RESEARCH AGENT — Skill Operacional v3.0

## 📚 Arquivos de Referência — Leia sob Demanda

| Módulo | Conteúdo | Quando Ler |
|---|---|---|
| `referencias/tools.md` | Assinaturas completas de todas as 15 tools | Ao usar tool não documentada aqui |
| `referencias/orchestrator.md` | Lógica interna do Orchestrator Python | Ao debugar falhas de pipeline interno |

---

## ⚡ SEÇÃO I — INFRAESTRUTURA

| Item | Valor |
|---|---|
| **Localização** | `E:\Meus LLMs\smart-research-agent` |
| **Porta MCP (SSE)** | `3458` |
| **Endpoint SSE** | `http://localhost:3458/mcp/sse` (⚠️ NÃO é `/sse` — FastMCP montado em `/mcp` via `sse_app()`) |
| **Endpoint REST** | `http://localhost:3458/research` (POST; fallback sem MCP) |
| **Dry-run custo** | `POST /research` com `{"query": "...", "dry_run": true}` → estimativa de custo/fontes sem buscar |
| **Endpoint Health** | `http://localhost:3458/health` |
| **Servidor MCP** | `smart-research-agent` (nome no mcp_config.json) |
| **Firecrawl Dep.** | `http://firecrawl-api-new:3002` (interno Docker, rede `firecrawl_backend`; ver `.env` do SRA) |

### Verificação de Saúde (Obrigatória antes de usar)

```powershell
# 1. Verificar se o container está rodando
docker ps --format "{{.Names}}" | Select-String "smart-research"

# 2. Checar health endpoint
Invoke-RestMethod -Uri "http://localhost:3458/health"
# Retorno esperado: { "status": "ok", "service": "smart-research-agent" }
```

### Ativar se não estiver rodando

```powershell
# Firecrawl DEVE subir primeiro (é dependência interna)
cd "E:\Meus LLMs\Firecrawl_New"; docker compose up -d

# Depois o smart-research-agent
cd "E:\Meus LLMs\smart-research-agent"; docker compose up -d

# Aguardar inicialização
Start-Sleep -Seconds 10
Invoke-RestMethod -Uri "http://localhost:3458/health"
```

---

## 🧰 SEÇÃO II — CATÁLOGO COMPLETO DE TOOLS MCP (18 TOOLS)

O agente expõe **18 tools** via servidor MCP SSE (montado em `/mcp`). Servidor: `smart-research-agent`.

### TOOL 1 — `research_technology` ★ (Principal — Modo Standard)

**Quando usar:** Pesquisa completa e automática. Pipeline de 9 passos internos — análise de intenção → expansão de queries → busca paralela em 8 fontes → ranking → confidence scoring → síntese → relatório Markdown.

```
Invoque: research_technology
  query: "CRM open source parecido com HubSpot"
```

| Parâmetro | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `query` | string | ✅ | Query em linguagem natural |

**Retorna:** Relatório Markdown completo com seções de overview, projetos encontrados, comparativos, código, fontes e metadados de confiança.

---

### TOOL 2 — `research_technology_v2` ★★ (Principal — Modo Avançado)

**Quando usar:** Pesquisa completa com suporte a deep research e confidence scoring explícito.

```
Invoque: research_technology_v2
  query: "melhor ORM Python para projetos assíncronos"
  mode: "deep"
  include_confidence: true
```

| Parâmetro | Tipo | Default | Descrição |
|---|---|---|---|
| `query` | string | — | Query em linguagem natural |
| `mode` | string | `"standard"` | `"standard"` \| `"deep"` — deep usa raciocínio em árvore |
| `include_confidence` | bool | `true` | Incluir confidence scores no output |

**Diferencial do modo `deep`:**
- Raciocínio em árvore (até 3 níveis de profundidade)
- Geração e teste de hipóteses concorrentes
- Relatório inclui seção `## Reasoning Tree` com ramificações exploradas
- Custo ~5-10x maior que `standard` — use apenas quando precisar de profundidade máxima

**Retorna:** Relatório Markdown estendido com `## Reasoning Tree`, `## Confidence Report` e seções padrão.

---

### TOOL 3 — `scrape_url` (Cascade Inteligente de Scrapers)

**Quando usar:** Extrair conteúdo de qualquer URL, incluindo páginas com JS pesado, CAPTCHAs, ou bloqueadas por WAF. Usa cascade automático: Firecrawl → Spider.cloud → Steel.dev → Jina Reader.

```
Invoque: scrape_url
  url: "https://site-com-js-pesado.com/pagina"
  force_browser: false
```

| Parâmetro | Tipo | Default | Descrição |
|---|---|---|---|
| `url` | string | — | URL a ser extraída |
| `force_browser` | bool | `false` | `true` força Steel.dev (browser real) |

**Lógica de Cascade (automática):**
1. **Firecrawl** (padrão — Markdown limpo) → timeout 10s
2. **Spider.cloud** (se Firecrawl timeout ou erro 429) → Rust-powered, ultra-rápido
3. **Steel.dev** (se Spider retorna vazio + JS detectado) → browser real com stealth
4. **Jina Reader** (`r.jina.ai/<url>`) → fallback final zero-setup

**Retorna:** Conteúdo extraído em Markdown com metadados de qual scraper foi usado.

---

### TOOL 4 — `confidence_check` (Anti-Hallucination)

**Quando usar:** Verificar a confiabilidade de uma afirmação antes de incluí-la em relatório ou resposta. Retorna confidence_score e evidence_quality.

```
Invoque: confidence_check
  claim: "SQLAlchemy é o ORM mais usado em Python"
  sources: ["https://github.com/sqlalchemy/sqlalchemy", "https://survey.stackoverflow.com/2025"]
```

| Parâmetro | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `claim` | string | ✅ | Afirmação a verificar |
| `sources` | list[string] | ✅ | URLs que supostamente suportam a afirmação |

**Retorna:**
```json
{
  "confidence_score": 0.87,
  "evidence_quality": "verified",
  "hallucination_flags": [],
  "citations": ["https://github.com/sqlalchemy/sqlalchemy"],
  "contradictions": []
}
```

**Níveis de `evidence_quality`:** `verified` | `cited` | `inferred` | `unknown`

---

### TOOL 5 — `search_github`

**Quando usar:** Encontrar bibliotecas, comparar projetos por stars/forks, descobrir projetos ativos de um ecossistema.

```
Invoque: search_github
  query: "self-hosted CRM python"
  domain: "saas_b2b"
  max_results: 15
```

| Parâmetro | Tipo | Default | Descrição |
|---|---|---|---|
| `query` | string | — | Termos de busca |
| `domain` | string | `general` | Contexto temático (ver Seção III) |
| `max_results` | int | `10` | Máx: 30 |

> ⚠️ **Resiliência:** O searcher limpa stop-words e limita a 4 termos principais antes de enviar à API GitHub. Se a query complexa falhar, executa fallback automático com `sort:stars`.

**Retorna:** JSON array com `title`, `url`, `description`, `source`, `metrics` (stars, forks, language).

---

### TOOL 6 — `search_reddit`

**Quando usar:** Opiniões orgânicas, relatos reais, comparativos feitos pela comunidade, threads de recomendação.

```
Invoque: search_reddit
  query: "best open source CRM reddit"
  domain: "saas_b2b"
  max_results: 10
```

**Retorna:** JSON array com `title`, `url`, `description`, `source`, `metrics` (subreddit, upvotes).

---

### TOOL 7 — `search_hackernews`

**Quando usar:** Tendências técnicas, debates sobre ferramentas emergentes, opiniões de engenheiros seniores, lançamentos tech.

```
Invoque: search_hackernews
  query: "self-hosted analytics LLM"
  domain: "dev_tools"
  max_results: 10
```

**Retorna:** JSON array com `title`, `url`, `description`, `source`, `metrics` (score, comments).

---

### TOOL 8 — `search_awesome_lists`

**Quando usar:** Descobrir ferramentas reconhecidas de um ecossistema, listas curadas, catálogo de opções por categoria.

```
Invoque: search_awesome_lists
  query: "self-hosted"
  domain: "general"
  max_results: 20
```

| Parâmetro | Tipo | Default | Max |
|---|---|---|---|
| `max_results` | int | `15` | `50` |

**Retorna:** JSON array com `title`, `url`, `description`, `source`.

---

### TOOL 9 — `search_arxiv`

**Quando usar:** Embasamento acadêmico sobre técnicas de IA/ML, papers sobre algoritmos, arquiteturas de modelos, pesquisas recentes.

```
Invoque: search_arxiv
  query: "RAG retrieval augmented generation"
  domain: "ai_ml"
  max_results: 10
```

| Parâmetro | Tipo | Default | Max |
|---|---|---|---|
| `max_results` | int | `10` | `20` |

**Retorna:** JSON array com `title`, `url`, `description` (resumo), `source`, `metrics` (autores, data).

---

### TOOL 10 — `search_producthunt`

**Quando usar:** Descobrir SaaS recentes, produtos inovadores, alternativas a ferramentas conhecidas, tendências de mercado.

```
Invoque: search_producthunt
  query: "AI writing tool"
  domain: "saas_b2b"
  max_results: 10
```

| Parâmetro | Tipo | Default | Max |
|---|---|---|---|
| `max_results` | int | `10` | `20` |

**Retorna:** JSON array com `title`, `url`, `description`, `source`, `metrics` (votos, data de lançamento).

---

### TOOL 11 — `search_web`

**Quando usar:** Busca web geral via DuckDuckGo/SerpAPI. Documentação de produtos, artigos, tutoriais, qualquer conteúdo público.

```
Invoque: search_web
  query: "como configurar n8n self-hosted Docker"
  domain: "general"
  max_results: 10
```

| Parâmetro | Tipo | Default | Max |
|---|---|---|---|
| `max_results` | int | `10` | `20` |

**Retorna:** JSON array com `title`, `url`, `description`, `source`.

---

### TOOL 12 — `scrape_with_firecrawl`

**Quando usar:** Extrair conteúdo completo de uma URL via Firecrawl interno. Sites com JavaScript, SPAs, páginas protegidas contra bots.

> ⚠️ **Pré-requisito:** Firecrawl Docker deve estar ativo na porta 3002.
> Para cascade inteligente, prefira `scrape_url` (TOOL 3).

```
Invoque: scrape_with_firecrawl
  query: "https://url-especifica.com/pagina"
  domain: "general"
  max_results: 5
```

| Parâmetro | Tipo | Default | Max | Observação |
|---|---|---|---|---|
| `query` | string | — | — | URL ou termo para o Firecrawl |
| `max_results` | int | `5` | `10` | Limita páginas do crawl |

**Retorna:** JSON array com conteúdo extraído in Markdown.

---

### TOOL 13 — `analyze_query_intent`

**Quando usar:** Antes de uma pesquisa manual, para entender domínio e intenção. Útil para planejar qual combinação de tools usar.

```
Invoque: analyze_query_intent
  query: "compare n8n vs Zapier 2026"
```

**Retorna:**
```json
{
  "domain": "automation",
  "intention": "compare",
  "entities": ["n8n", "Zapier"],
  "urgency": "nao",
  "confidence": "alta"
}
```

**Domínios disponíveis:** `saas_b2b` | `dev_tools` | `ai_ml` | `automation` | `infrastructure` | `open_source` | `general`

**Intenções:** `discover` | `compare` | `learn` | `implement` | `evaluate`

---

### TOOL 14 — `expand_query`

**Quando usar:** Gerar variações otimizadas de uma query antes de executar buscas manuais em múltiplas tools individuais.

```
Invoque: expand_query
  query: "CRM open source"
```

**Retorna:** JSON array com queries expandidas (8-12 variações com prioridade, tipo e estratégia):

```json
[
  {
    "query": "self-hosted CRM python django",
    "type": "qualificador",
    "priority": 9,
    "rationale": "Adiciona stack técnico para refinar resultados no GitHub"
  }
}
```

**Tipos de expansão:** `sinonimo` | `qualificador` | `plataforma` | `comparacao` | `caso_de_uso` | `gap_fill` | `evidence` | `community`

---

### TOOL 15 — `record_feedback` (Feedback Loop)

**Quando usar:** Registrar feedback positivo ou negativo sobre um resultado específico para melhorar o rankeamento futuro de resultados similares em queries relacionadas.

```
Invoque: record_feedback
  result_id: "https://github.com/monicahq/monica"
  signal: "up"
  query: "CRM open source 2026"
```

| Parâmetro | Tipo | Obrigatório | Valores | Descrição |
|---|---|---|---|---|
| `result_id` | string | ✅ | qualquer string | ID do resultado (URL, título ou hash) |
| `signal` | string | ✅ | `useful` \| `bookmark` \| `not_useful` \| `irrelevant` \| `outdated` | Aliases aceitos: `up`→useful, `down`→not_useful (v3.1.2) |
| `query` | string | ❌ | — | Query original (melhora aprendizado contextual) |

**Retorna:**
```json
{
  "status": "ok",
  "result_id": "...",
  "signal": "up",
  "message": "Feedback registrado. Rankeamento atualizado."
}
```

> ⚡ **Mecanismo:** `FeedbackStore` (SQLite) + `FeedbackRanker` ajusta automaticamente os pesos dos resultados em queries futuras similares.

---

### TOOL 16 — `search_anything` ★ (Canivete Suíço — Busca Universal)

**Quando usar:** Busca multi-fonte RÁPIDA (JSON bruto) quando você não sabe qual tool específica usar. Diferente de `research_technology` (pipeline completo + relatório), esta escolhe as fontes automaticamente via SourcePlanner e retorna resultados normalizados.

```
Invoque: search_anything
  query: "livros sobre estoicismo"
  hint_domain: "general"   # opcional: ai_ml, infrastructure, etc.
  max_results: 10          # por fonte, máx 30
```

**Retorna:** JSON com `query`, `domain`, `sources_queried`, `total`, `results[]` ({title, url, description, source}). Fontes genéricas do catálogo YAML (open_library, openalex, osm_nominatim...) participam automaticamente.

---

### TOOL 17 — `monitor_topic` (Vigília Contínua)

**Quando usar:** Monitorar um tópico ao longo do tempo com buscas periódicas, retornando apenas incrementos (novas entidades/fontes/seções). Usa o ResearchScheduler; jobs vivem em `reports/monitors`.

```
Invoque: monitor_topic
  action: "create"          # create | check | list | delete
  topic: "LangGraph releases"
  check_interval_minutes: 60
  monitor_id: "<id>"        # obrigatório para check/delete
```

---

### TOOL 18 — `get_trending` (Trending Global via GDELT)

**Quando usar:** Descobrir os tópicos/notícias com maior volume de cobertura global nas últimas N horas, sem query e sem API key.

```
Invoque: get_trending
  hours: 24          # janela temporal (default: 24)
  max_records: 10    # máx: 20
```

---

## 🗺️ SEÇÃO III — DOMÍNIOS VÁLIDOS

| Valor | Use Para |
|---|---|
| `saas_b2b` | Produtos SaaS, CRMs, ERPs, ferramentas de negócio |
| `dev_tools` | Frameworks, bibliotecas, IDEs, DevOps |
| `ai_ml` | Modelos de IA, LLMs, pipelines de ML, MLOps |
| `automation` | n8n, Make, Zapier, RPA, workflows |
| `infrastructure` | Cloud, Docker, Kubernetes, servidores |
| `open_source` | Alternativas self-hosted, projetos open source em geral |
| `general` | Qualquer coisa que não se enquadre nas acima |

---

## 🧭 SEÇÃO IV — MATRIZ DE DECISÃO: QUAL TOOL USAR?

| Cenário | Tool Recomendada |
|---|---|
| Pesquisa completa e aprofundada (padrão) | `research_technology` ★ |
| Pesquisa completa com deep research / reasoning tree | `research_technology_v2` com `mode: "deep"` ★★ |
| Verificar confiabilidade de uma afirmação | `confidence_check` |
| Extrair conteúdo de URL (qualquer tipo) | `scrape_url` (cascade automático) |
| Extrair URL específica via Firecrawl direto | `scrape_with_firecrawl` |
| Encontrar repositórios, comparar por stars | `search_github` |
| Opiniões reais da comunidade | `search_reddit` |
| Tendências técnicas, debates de engenheiros | `search_hackernews` |
| Catálogo curado de ferramentas | `search_awesome_lists` |
| Papers acadêmicos de IA/ML | `search_arxiv` |
| Produtos SaaS recentes, lançamentos | `search_producthunt` |
| Documentação, tutoriais, artigos | `search_web` |
| Entender domínio/intenção antes de pesquisar | `analyze_query_intent` |
| Gerar variações de query para buscas manuais | `expand_query` |
| Busca multi-fonte rápida sem saber qual tool usar | `search_anything` ★ |
| Monitorar tópico continuamente (vigília) | `monitor_topic` |
| Descobrir o que está em alta globalmente agora | `get_trending` |
| Registrar feedback sobre relevância de um resultado | `record_feedback` |

---

## ⚙️ SEÇÃO V — PIPELINES DE EXECUÇÃO

### Pipeline Standard (Automático)

```
research_technology → relatório completo em 1 chamada
```

### Pipeline Deep Research (Máxima Profundidade)

```
research_technology_v2 (mode: "deep") → relatório com reasoning tree
```

### Pipeline Manual com Anti-Hallucination

```
1. analyze_query_intent  → detectar domínio e intenção
2. expand_query          → gerar 8-12 variações de query
3. search_github         → repositórios relevantes
4. search_reddit         → opiniões da comunidade
5. search_hackernews     → debates técnicos
6. search_awesome_lists  → catálogo curado
7. scrape_url            → extrair conteúdo das URLs mais relevantes (cascade)
8. confidence_check      → verificar afirmações críticas antes de incluir
9. Sintetizar em relatório estruturado
```

### Pipeline de Scraping com Fallback

```
scrape_url (force_browser=false)  → Firecrawl (padrão)
  ↓ timeout ou erro 429
  → Spider.cloud (Rust, ultra-rápido)
  ↓ conteúdo vazio + JS detectado
  → Steel.dev (browser real)
  ↓ falha
  → Jina Reader (r.jina.ai/<url>)
```

**📌 ANTES de scrape_url: validar URL com firecrawl_map (novo em 2026-08)**

Páginas de integração (ex: `/integrations/whatsapp.html`) são movidas/renomeadas com frequência, causando 404. Regra de ouro:

1. **Se a URL foi "adivinhada"** → use `firecrawl_map` para validar primeiro:
   - `firecrawl_map("https://www.zoho.com", search="whatsapp integration")`
   - Revise os URLs retornados, pegue o correto, depois `scrape_url`
2. **Se a URL é canônica (da search_web)** → pode scrape direto.
3. **404 após scrape** → volte para `firecrawl_map` ou `search_web` para redirecionar.

---

## 🧠 SEÇÃO VI — CAPACIDADES AVANÇADAS (ARES-V4.2)

### OrvixMemory — Memória Persistente entre Pesquisas

| Capacidade | Detalhe |
|---|---|
| **Arquivo** | `.research_memory.db` (criado automaticamente no container) |
| **Algoritmo** | RRF — Reciprocal Rank Fusion de BM25 + busca vetorial + grafo de conceitos |
| **Benefício** | Pesquisas relacionadas ganham contexto histórico injetado automaticamente no prompt |
| **Transparência** | O relatório inclui seção `## Memory Context` quando memória relevante é encontrada |

### SmartModelRouter — Roteamento por Custo

| Tier | Modelo | Usado Para |
|---|---|---|
| **T1 (Free)** | Groq Llama 3.1 8B | Análise de intenção, expansão de queries, classificações simples |
| **T2** | Groq Llama 3.3 70B | Síntese intermediária, reranking de resultados |
| **T3** | OpenRouter (variados) | Relatórios finais quando T1/T2 são insuficientes |

> ⚠️ Configure `GROQ_API_KEY` e `OPENROUTER_API_KEY` no `.env` do smart-research-agent para ativar o roteamento por custo.

### RSS Searcher — 15 Feeds Curados

Feeds monitorados e injetados automaticamente no pipeline `research_technology`: HackerNews, GitHub Trending, Dev.to, The Verge Tech, Ars Technica, MIT Technology Review, Google AI Blog, OpenAI News, Anthropic Blog, Hugging Face Blog + 5 feeds domain-específicos.

### Hook Anti-Query-Vaga

| Comportamento | Detalhe |
|---|---|
| **Queries bloqueadas** | <3 tokens ou muito genéricas (ex: `"IA"`, `"ferramentas"`) — rejeitadas com sugestão de refinamento |
| **Whitelist** | Comandos Git/admin, termos técnicos únicos — liberados automaticamente |

### Veredito Rico — Formato de Saída Enriquecido

Os relatórios agora incluem bloco de veredito no topo:

- 🔴 **FOCA** em: `[ferramenta principal recomendada]`
- 🟡 **CONSIDERA** também: `[alternativas sólidas]`
- ⚪ **ACOMPANHA** (maturidade): `[projetos promissores mas imaturos]`
- ⛔ **IGNORA**: `[soluções descartadas com justificativa]`
- `⏱ Leitura: ~X min | 📊 Fontes: N | 🔢 Confidence: X%`

---

## ⚠️ SEÇÃO VII — TRATAMENTO DE ERROS

| Erro | Causa | Ação |
|---|---|---|
| `ECONNREFUSED 3458` | Container parado | `cd "E:\Meus LLMs\smart-research-agent"; docker compose up -d` (ou rode `ATALHOS_AG\START_SmartResearch.bat`, que sobe SRA + Firecrawl juntos) |
| `404` em `/sse` | Path errado — FastMCP montado em `/mcp` | Usar `http://localhost:3458/mcp/sse` |
| Sessão MCP "trava" sem resposta | Container fora do ar e bridge antiga em loop infinito de retry | Corrigido (2026-08-24): `mcp-server.mjs` desiste após 10 tentativas com timeout 10s; verifique container e reinicie a IDE |
| Pipeline demora ~5min sem responder | HITL aguardando aprovação humana | Corrigido (2026-08-24): `hitl_enabled=False` por default no `Config`; se reativar, `hitl_timeout_seconds=30` |
| Relatório sai com fonte única (ex.: só arxiv) e 20 resultados genéricos | Fontes github/reddit/awesome estouraram timeout do SearchStage sob carga paralela; circuit breaker abriu | Corrigido (2026-08-24): `SOURCE_TIMEOUT_MAP` em `search_stage.py` — github/reddit/awesome 8-10s→20s. Se persistir, checar conectividade: `docker exec sra-app python -c "import httpx; print(httpx.get('https://api.github.com/search/repositories?q=test', timeout=15).status_code)"` |
| Síntese pobre / relatório genérico | Cadeia de failover LLM degradada (ver logs `[Failover]`/`[RotaçãoChaves]`) | Renovar chaves no `.env`: deepseek/github_models (conexão), gemini (quota diária), openrouter (chaves com 401 `User not found`). O pipeline NÃO trava mais — degrada para fallback |
| Reddit retorna vazio/403 | Reddit bloqueia requests de datacenter sem OAuth | Usar Firecrawl como fonte reddit (fallback já integrado no `reddit_searcher.py`) ou configurar credenciais OAuth do Reddit |
| Resultado vazio no `search_web` | SerpAPI/DuckDuckGo offline | Usar `scrape_url` ou `scrape_with_firecrawl` as fallback |
| `research_technology` retorna relatório pobre | Query muito genérica | Usar pipeline manual (Seção V) com queries específicas |
| `search_github` retorna 0 resultados | Query com muitos qualificadores | Simplificar — o searcher já aplica limpeza automática; tentar query mais curta |
| `scrape_url` retorna conteúdo vazio | Todos os scrapers falharam | Usar `firecrawl_scrape` via MCP direto do Firecrawl com `timeout: 30000` |
| `confidence_check` retorna score < 0.3 | Afirmação sem suporte nas fontes | Não incluir a afirmação no relatório sem mais evidências |

---

## 📋 HISTÓRICO DE VERSÕES

| Data | Versão | Mudanças |
|---|---|---|
| 2026-06-17 | v1.0 | Criação inicial — 11 tools documentadas, matriz de decisão, pipeline manual, tratamento de erros. |
| 2026-06-18 | v2.0 | Adicionadas 3 novas tools: `research_technology_v2` (deep research mode + reasoning tree), `scrape_url` (cascade inteligente Spider→Steel→Jina), `confidence_check` (anti-hallucination scoring). Atualizada lógica de resiliência do `search_github`. Novos pipelines documentados. |
| 2026-06-24 | v3.0 | **ARES-V4.2:** Adicionada TOOL 15 `record_feedback` (Feedback Loop). Nova SEÇÃO VI documentando: OrvixMemory (SQLite RRF BM25+Vetor+Grafo), SmartModelRouter (tiers T1/T2/T3 Groq→OpenRouter), RSS Searcher (15 feeds curados), Hook Anti-Query-Vaga (whitelist Git/admin), Veredito Rico (FOCA/CONSIDERA/ACOMPANHA/IGNORA). Atualizada matriz de decisão e referências de contagem. |
| 2026-08-24 | v3.1 | **Auditoria contra o código real (container):** 15→18 tools — adicionadas TOOL 16 `search_anything` (busca universal multi-fonte), TOOL 17 `monitor_topic` (vigília contínua via ResearchScheduler) e TOOL 18 `get_trending` (GDELT). Corrigido endpoint SSE: `/sse`→**`/mcp/sse`** (FastMCP montado via `sse_app()` em `/mcp`). Documentado dry-run de custo no POST `/research`. Notas anti-travamento: HITL desativado por default, failover imediato em erro de conexão no LLMClient, bridge MCP com limite de 10 tentativas. |
| 2026-08-24 | v3.1.1 | **Learnings de uso real (primeira pesquisa pós-fix):** (1) Cold start do orchestrator no primeiro request pode levar >60s (ChromaDB+KuzuDB lazy init) — usar timeout ≥240s na primeira chamada. (2) `SOURCE_TIMEOUT_MAP`: github/reddit/awesome elevados para 20s — timeouts de 8-10s abriam circuit breaker sob 12 queries paralelas e o relatório saía só com arxiv. (3) Diagnóstico rápido de cadeia LLM: `docker logs sra-app | Select-String "Failover|RotaçãoChaves"` — chaves mortas degradam síntese mas não travam (failover funciona). (4) Reddit direto retorna 403 de datacenter — confiar no fallback Firecrawl do `reddit_searcher`. Pipeline real medido: ~170s modo cirurgia, 10 queries expandidas, 9 fontes planejadas, 15 stages ReAct. |
| 2026-08-24 | v3.1.2 | **Bateria de testes de estresse (32 searchers, 18 tools MCP, failover):** 4 bugs encontrados e corrigidos no código: (1) `FirecrawlClient._normalize_search_results` lia `results.data` mas o SDK firecrawl-py>=4.30 retorna `SearchData.web` → WebSearcher devolvia itens com título/url VAZIOS; agora cobre web/news/images + descarta placeholders. (2) `_get_trending_impl` retornava `{"error": ""}` porque str(ConnectTimeout) é vazia → agora inclui o tipo da exceção. (3) `record_feedback` rejeitava "up"/"down" documentados na skill → aliases up→useful, down→not_useful. (4) ClinicalTrials enviava `sort=relevance` inválido à API v2 → HTTP 400 sempre; removido. Notas: GDELT dá ConnectTimeout de dentro do container (rede local — funciona do host); ProductHunt exige `PRODUCTHUNT_TOKEN` no .env (skip gracioso sem ele); KuzuDB aceita 1 processo por lock — scripts externos devem instanciar searchers via SearcherFactory, não o orchestrator completo. Query FOCALIZADA validou qualidade: Mem0(23)/Zep(29)/Letta(18)/Cognee(13) menções em 40s vs mega-query que zerava os líderes do domínio. |
