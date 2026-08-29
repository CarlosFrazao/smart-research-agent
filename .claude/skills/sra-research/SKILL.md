---
name: sra-research
description: >
  Sub-skill especializada do Smart Research Agent para execução de pesquisas
  profundas automatizadas. Cobre configuração de modo de operação (OperationModes),
  cache compartilhado Redis (SharedCache), e relatório com veredito rico e confidence
  scoring. Usa research_technology_v2 com modos guerrilha/cirurgia/arqueologia/concorrencia.
version: 1.0
tags: [research, sra, automation, antigravity]
---

# 🔎 SRA-RESEARCH — Sub-skill de Pesquisa Profunda

Sub-skill atômica do ecossistema Antigravity para **pesquisa automatizada** via Smart Research Agent.
Herdada de: `smart-research-agent` (v3.0, 15 tools MCP).

---

## ⚡ ATIVAÇÃO RÁPIDA

### Pré-requisitos
```powershell
# 1. Verificar infraestrutura
Invoke-RestMethod -Uri "http://localhost:3458/health"
# Esperado: { "status": "ok" }

# 2. Se inativo, subir containers
cd "E:\Meus LLMs\Firecrawl_New"; docker compose up -d
cd "E:\Meus LLMs\smart-research-agent"; docker compose up -d
Start-Sleep -Seconds 10
```

---

## 🧠 MODOS DE OPERAÇÃO (NOVOS — Fase 4)

| Modo | Quando Usar | Fontes Ativas | Custo |
|---|---|---|---|
| `guerrilha` | Resposta rápida, ampla cobertura | Todas (sem Arxiv/Wayback) | ⭐ |
| `cirurgia` | Análise técnica profunda | GitHub, ArXiv, Wayback, Firecrawl | ⭐⭐⭐ |
| `radar` | Monitoramento de tendências | Reddit, HackerNews, ProductHunt, RSS | ⭐ |
| `arqueologia` | Código legado, histórico | Wayback, GitHub, StackOverflow | ⭐⭐ |
| `concorrencia` | Comparação competitiva | GitHub, ProductHunt, Reddit, Web | ⭐⭐ |
| `black_ops` | Máxima profundidade | Todas | ⭐⭐⭐⭐ |
| `auto` | Detecção automática | Baseado em keywords da query | variável |

### Aplicar Modo via MCP

```
# Via research_technology_v2
Invoque: research_technology_v2
  query: "comparar Supabase vs Neon para projetos Python"
  mode: "deep"
  op_mode: "concorrencia"
  include_confidence: true
```

### Aplicar Modo via CLI

```bash
python -m src.main research \
  --query "comparar Supabase vs Neon" \
  --op-mode concorrencia \
  --deep
```

---

## 📋 SELEÇÃO DE MODO (HEURÍSTICA AUTOMÁTICA)

Use `op_mode: "auto"` quando não souber qual modo aplicar.
O sistema analisa keywords da query:

| Keywords na Query | Modo Selecionado |
|---|---|
| "rápido", "urgente", "resumo", "lista" | `guerrilha` |
| "implementar", "código", "técnico", "arquitetura" | `cirurgia` |
| "tendência", "novidade", "novo", "2026" | `radar` |
| "legado", "antigo", "histórico", "deprecated" | `arqueologia` |
| "vs", "comparar", "alternativa", "concorrente" | `concorrencia` |
| "aprofundar", "completo", "tudo sobre" | `black_ops` |

---

## 🔗 CACHE COMPARTILHADO (SharedCache — Fase 5)

Resultados de pesquisa são automaticamente cacheados no Redis.
Cache key = SHA256(query.lower().strip())[:16]

| Estratégia | TTL | Usado No Modo |
|---|---|---|
| `aggressive` | 7 dias | guerrilha, radar |
| `moderate` | 48 horas | concorrencia (padrão) |
| `minimal` | 1 hora | cirurgia, black_ops |
| `permanent` | 30 dias | arqueologia |

**Para forçar nova pesquisa (ignorar cache):**
```python
# No orquestrador interno — não disponível via MCP diretamente
# Use uma query ligeiramente diferente para obter resultado fresco
query = "Supabase vs Neon para Python — análise junho 2026"
```

---

## 🎯 PIPELINES RECOMENDADOS

### Pipeline Rápido (Guerrilha)
```
1. research_technology  → op_mode: "guerrilha"
   Resultado: relatório em ~30s, fontes amplas, sem profundidade técnica
```

### Pipeline Técnico Profundo (Cirurgia)
```
1. analyze_query_intent          → entender domínio técnico
2. expand_query                  → gerar 8-12 variações técnicas
3. research_technology_v2        → op_mode: "cirurgia", mode: "deep"
4. confidence_check              → validar afirmações críticas
   Resultado: relatório técnico com árvore de raciocínio + citations
```

### Pipeline Competitivo (Concorrencia)
```
1. research_technology_v2        → op_mode: "concorrencia"
2. search_github (comparativo)   → métricas stars/forks/atividade
3. search_producthunt            → recepção de mercado
4. confidence_check              → validar dados de pricing/features
   Resultado: matriz comparativa com veredito FOCA/CONSIDERA/IGNORA
```

---

## 📊 LEITURA DO VEREDITO RICO

Todo resultado de `research_technology` inclui:

```
🔴 FOCA: [solução principal recomendada]
🟡 CONSIDERA: [alternativas sólidas]
⚪ ACOMPANHA: [projetos emergentes]
⛔ IGNORA: [soluções descartadas]
⏱ Leitura: ~X min | 📊 Fontes: N | 🔢 Confidence: X%
```

Confidence scoring:
- `≥ 80%` → Resultado confiável, use diretamente
- `50-79%` → Verificar manualmente as fontes citadas
- `< 50%` → Executar `confidence_check` nas afirmações principais

---

## ⚠️ ERROS COMUNS

| Erro | Causa | Solução |
|---|---|---|
| `ECONNREFUSED 3458` | Container parado | `docker compose up -d` em smart-research-agent |
| Resultado pobre em black_ops | Timeout interno | Dividir em 2 queries: uma técnica + uma competitiva |
| Cache retornando resultado antigo | TTL não expirado | Variar a query com data/contexto adicional |
| Confidence < 30% | Query ambígua | Refinar com `analyze_query_intent` antes |

---

## 📋 HISTÓRICO

| Versão | Data | Mudanças |
|---|---|---|
| v1.0 | 2026-06-30 | Criação — Integra OperationModes + SharedCache + Veredito Rico |
