---
name: sra-scrape
description: >
  Sub-skill especializada do Smart Research Agent para extração de conteúdo web
  via cascade inteligente de scrapers (Firecrawl → Spider.cloud → Steel.dev → Jina Reader).
  Integra SharedCache Redis para evitar chamadas redundantes. Cobre detecção automática
  de JS, bypass de WAF, e extração estruturada de Markdown para pipelines RAG e análise.
version: 1.0
tags: [scraping, sra, firecrawl, cache, antigravity]
---

# 🕷️ SRA-SCRAPE — Sub-skill de Extração de Conteúdo Web

Sub-skill atômica do ecossistema Antigravity para **scraping robusto com cache**.
Cascade automático: Firecrawl → Spider.cloud → Steel.dev → Jina Reader.

---

## ⚡ INFRAESTRUTURA

| Componente | Localização | Porta |
|---|---|---|
| Firecrawl | Docker interno | 3002 (interno) |
| Spider.cloud | API externa | - |
| Steel.dev | API externa | - |
| Jina Reader | `r.jina.ai/<url>` | 443 |
| SharedCache | Redis | 6379 (interno) |

```powershell
# Verificar Firecrawl (necessário para scrape primário)
Invoke-RestMethod -Uri "http://localhost:3458/health"
```

---

## 🔗 CASCADE DE SCRAPERS (AUTOMÁTICO)

```
scrape_url(url)
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ NÍVEL 1: Firecrawl                                          │
│  • Markdown limpo + metadados                               │
│  • Timeout: 10s                                             │
│  • Falha se: timeout, 429, ECONNREFUSED                     │
└─────────────────────────────────────────────────────────────┘
    │ (falha)
    ▼
┌─────────────────────────────────────────────────────────────┐
│ NÍVEL 2: Spider.cloud                                       │
│  • Rust-powered, ultra-rápido                               │
│  • Melhor para: sites simples, HTML estático                │
│  • Falha se: conteúdo vazio + JS detectado                  │
└─────────────────────────────────────────────────────────────┘
    │ (falha)
    ▼
┌─────────────────────────────────────────────────────────────┐
│ NÍVEL 3: Steel.dev (Browser Real + Stealth)                │
│  • Playwright headless com anti-fingerprint                 │
│  • Melhor para: SPAs, sites com CAPTCHA, JS pesado          │
│  • Ativar diretamente: force_browser: true                  │
│  • Falha se: bloqueio CAPTCHA manual, rateLimit             │
└─────────────────────────────────────────────────────────────┘
    │ (falha)
    ▼
┌─────────────────────────────────────────────────────────────┐
│ NÍVEL 4: Jina Reader (zero-setup)                           │
│  • r.jina.ai/<url> — nenhuma API key necessária             │
│  • Fallback final sempre disponível                         │
│  • Limitação: sem JavaScript, sem conteúdo dinâmico         │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 INVOCAÇÃO VIA MCP

### Scraping Padrão (Cascade Automático)

```
Invoque: scrape_url
  url: "https://docs.pydantic.dev/latest/concepts/models/"
  force_browser: false
```

### Forçar Browser (JS Pesado / CAPTCHA)

```
Invoque: scrape_url
  url: "https://dashboard.com/app"
  force_browser: true
```

### Scraping via Firecrawl Direto (Sem Cascade)

```
Invoque: scrape_with_firecrawl
  query: "https://exemplo.com/pagina"
  domain: "general"
  max_results: 5
```

---

## 💾 INTEGRAÇÃO COM SHARED CACHE (Fase 5)

O `scrape_url` verifica automaticamente o cache Redis antes de chamar qualquer scraper:

```
Fluxo com Cache:
  1. Gera cache_key = "scrape:" + SHA256(url)[:16]
  2. Verifica Redis → HIT → retorna conteúdo cacheado (sem chamada de rede)
  3. MISS → executa cascade → salva no Redis com TTL
```

| Estratégia de TTL | TTL | Uso |
|---|---|---|
| `moderate` (padrão) | 48h | Documentação técnica, artigos |
| `aggressive` | 7 dias | Sites estáticos, blogs históricos |
| `minimal` | 1 hora | Painéis, dashboards, dados em tempo real |
| `permanent` | 30 dias | Páginas arquivadas, Wayback Machine |

**Para verificar hit/miss em log:**
```python
# SharedCache loga automaticamente:
# "SharedCache SCRAPE HIT: https://..."
# "SharedCache SCRAPE SET: https://... [moderate]"
```

---

## 📋 SELEÇÃO DO SCRAPER CERTO

| Tipo de Site | Recomendação | Parâmetro |
|---|---|---|
| Documentação (ReadTheDocs, Gitbook) | Firecrawl (padrão) | `force_browser: false` |
| Blog / Artigo com HTML simples | Spider.cloud | automático |
| SPA React/Vue/Angular | Steel.dev | `force_browser: true` |
| GitHub raw, Gists, Pastebin | Jina Reader | automático (fallback) |
| Wayback Machine archives | `search_wayback` ou Jina | manual |
| Sites com WAF agressivo | Steel.dev | `force_browser: true` |

---

## 🧩 PIPELINE SCRAPING + RAG

Para extrair conteúdo e injetar em contexto RAG:

```
1. expand_query             → gerar URLs relevantes de documentação
2. scrape_url (para cada)   → extrair em Markdown (cached)
3. SharedCache.get()        → verificar se já temos conteúdo fresco
4. TokenEconomy.truncate()  → truncar head+tail para caber no contexto
5. OrvixMemory.add()        → indexar no grafo de memória para futuras queries
```

---

## ⚠️ TRATAMENTO DE ERROS

| Erro | Causa | Ação |
|---|---|---|
| `scrape_url` retorna vazio em todos os níveis | Site totalmente bloqueado | Tentar `firecrawl_scrape` via MCP direto com `timeout: 30000` |
| `Steel.dev: 429 Too Many Requests` | Rate limit da API | Aguardar 60s e tentar Jina Reader diretamente |
| `Firecrawl: ECONNREFUSED` | Container Firecrawl parado | `cd "E:\Meus LLMs\Firecrawl_New"; docker compose up -d` |
| Conteúdo retornado é HTML bruto (sem Markdown) | Firecrawl retornou fallback | O cascade extrai texto bruto quando Markdown falha — verificar `scraper_used` no metadata |
| Cache hit retornando conteúdo desatualizado | TTL longo demais | Usar `minimal` ou invalidar via `SharedCache.delete(key)` |

---

## 🔍 INSPECIONAR METADATA DO SCRAPING

O `scrape_url` retorna metadata com qual scraper foi usado:

```json
{
  "content": "# Título da Página\n\nConteúdo em Markdown...",
  "metadata": {
    "url": "https://exemplo.com",
    "scraper_used": "firecrawl",
    "cached": false,
    "extracted_at": "2026-06-30T05:00:00Z",
    "content_length": 12430
  }
}
```

**scrapers_used válidos:** `firecrawl` | `spider` | `steel` | `jina` | `cache`

---

## 📋 HISTÓRICO

| Versão | Data | Mudanças |
|---|---|---|
| v1.0 | 2026-06-30 | Criação — Cascade 4 níveis + SharedCache + Pipeline RAG |
