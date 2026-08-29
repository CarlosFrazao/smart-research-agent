---
name: sra-verify
description: >
  Sub-skill especializada do Smart Research Agent para verificação anti-alucinação
  de afirmações técnicas. Usa confidence_check (MCP) + ResearchAuditor (Fase 4)
  para validar claims, cruzar fontes, detectar gaps e acionar re-pesquisa recursiva.
  Pipeline de 3 camadas: scoring → gap detection → re-research loop (máx 3 iterações).
version: 1.0
tags: [verification, anti-hallucination, sra, auditor, antigravity]
---

# ✅ SRA-VERIFY — Sub-skill de Verificação Anti-Alucinação

Sub-skill atômica do ecossistema Antigravity para **validação de claims e verificação de confiabilidade**.
Integra: `confidence_check` (MCP) + `ResearchAuditor` (Fase 4) + `TokenEconomy` (Fase 5).

---

## ⚡ QUANDO USAR

| Situação | Action |
|---|---|
| Antes de incluir dado técnico em relatório | `confidence_check` |
| Após pesquisa com confiança < 60% | `ResearchAuditor` completo |
| Para validar afirmação de pricing/stats | `confidence_check` + fonte primária |
| Relatório final com claims críticas | Pipeline Auditoria Completa (Seção III) |
| Afirmação que contradiz o que você sabe | `confidence_check` com múltiplas fontes |

---

## 🔍 SEÇÃO I — FERRAMENTA `confidence_check` (MCP)

### Invocação Básica

```
Invoque: confidence_check
  claim: "Supabase usa PostgreSQL como banco de dados principal"
  sources: ["https://supabase.com/docs", "https://github.com/supabase/supabase"]
```

### Parâmetros

| Parâmetro | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `claim` | string | ✅ | Afirmação a verificar (1 claim por chamada) |
| `sources` | list[string] | ✅ | URLs que supostamente suportam a afirmação |

### Retorno

```json
{
  "confidence_score": 0.92,
  "evidence_quality": "verified",
  "hallucination_flags": [],
  "citations": ["https://supabase.com/docs/guides/database/overview"],
  "contradictions": []
}
```

### Interpretação do Score

| Score | evidence_quality | Ação |
|---|---|---|
| `≥ 0.80` | `verified` | ✅ Incluir no relatório |
| `0.60–0.79` | `cited` | 🟡 Incluir com ressalva "fonte verificada" |
| `0.30–0.59` | `inferred` | ⚠️ Re-pesquisar com query mais específica |
| `< 0.30` | `unknown` | ❌ Remover afirmação ou marcar como não verificada |

---

## 🧠 SEÇÃO II — `ResearchAuditor` (Fase 4 — Auditoria Profunda)

O `ResearchAuditor` extrai **todas as claims** de um relatório e valida cada uma.

### Como Funciona Internamente

```
Relatório Markdown
    │
    ▼
[Extração de Claims] → identifica afirmações técnicas, stats, comparações
    │
    ▼
[Cruzamento de Fontes] → cada claim é cruzada com fontes do relatório
    │
    ▼
[Detecção de Gaps] → identifica claims sem fonte suficiente
    │
    ▼
[Re-pesquisa Recursiva] → para gaps: até 3 iterações de busca adicional
    │
    ▼
[Relatório Auditado] → confiança geral + claims marcadas individualmente
```

### Ativar via Orchestrator (CLI)

```bash
python -m src.main research \
  --query "comparar Redis vs Memcached 2026" \
  --audit \
  --max-audit-iterations 3
```

### Ativar via MCP

```
# Op-mode com auditoria ativa (qualquer modo exceto "guerrilha")
Invoque: research_technology_v2
  query: "Redis vs Memcached performance 2026"
  op_mode: "cirurgia"
  include_confidence: true
```
> A auditoria é executada automaticamente quando `include_confidence: true` + op_mode ≠ "guerrilha".

---

## 🔄 SEÇÃO III — PIPELINE DE AUDITORIA COMPLETA

Para relatórios críticos (dados de negócio, pricing, performance benchmarks):

### Passo 1 — Pesquisa Inicial
```
research_technology_v2
  query: [sua query]
  mode: "deep"
  include_confidence: true
  op_mode: "cirurgia"
```

### Passo 2 — Extrair e Validar Claims Críticas
```
Para cada afirmação técnica/statística no relatório:

  confidence_check
    claim: "[afirmação extraída]"
    sources: ["[url citada no relatório]", "[url secundária encontrada]"]

  → Registrar score + evidence_quality
```

### Passo 3 — Tratar Gaps (Score < 0.60)
```
Para claims com score < 0.60:

  search_web
    query: "[claim específica] site:docs OR site:github OR filetype:pdf"

  OU

  scrape_url
    url: "[fonte primária encontrada]"

  → Re-executar confidence_check com novas fontes
```

### Passo 4 — Registrar Feedback para Aprendizado
```
Para URLs que confirmaram claims com score ≥ 0.80:

  record_feedback
    result_id: "[url confirmadora]"
    signal: "up"
    query: "[query original]"
```

### Passo 5 — Relatório Final com Certificação
```
Adicionar bloco no início do relatório:

## ✅ Verificação de Confiabilidade
- Claims totais analisadas: N
- Verificadas (≥0.80): X
- Parcialmente verificadas (0.60-0.79): Y
- Não verificadas (<0.60): Z — [lista com ação]
- Confiança geral: X%
- Auditoria executada por: ResearchAuditor v1.0 + confidence_check
```

---

## ⚙️ SEÇÃO IV — TokenEconomy no Loop de Verificação

Para evitar estouro de budget em auditorias de relatórios longos:

```python
# Verificar antes de processar claim em texto muito longo
from src.token_economy import TokenEconomy

te = TokenEconomy(default_model="claude-sonnet-3.5")

# Truncar texto do relatório antes de extrair claims
truncated = te.smart_truncate(huge_report, max_tokens=6000, head_ratio=0.7)

# Verificar se a chamada está no budget
if te.check_budget(truncated, model="claude-sonnet-3.5"):
    # ... executar auditoria
    pass

# Verificar custo da sessão
summary = te.session_summary()
print(f"Gasto total: ${summary['total_cost_usd']:.4f}")
```

### Limites Padrão (Budget)

| Limite | Valor Padrão | Configurável |
|---|---|---|
| Tokens por chamada | 8.000 | `Budget(max_tokens_per_call=N)` |
| Custo por chamada | $0,05 | `Budget(max_cost_usd_per_call=N)` |
| Custo total da sessão | $2,00 | `Budget(max_cost_usd_session=N)` |

---

## 🏷️ NÍVEIS DE CERTIFICAÇÃO

Use estes labels ao reportar claims verificadas:

| Label | Critério | Uso |
|---|---|---|
| `[VERIFIED ✅]` | Score ≥ 0.80 + fonte primária | Afirmações confirmadas |
| `[CITED 🟡]` | Score 0.60–0.79 | Afirmações com suporte parcial |
| `[INFERRED ⚠️]` | Score 0.30–0.59 | Afirmações derivadas de contexto |
| `[UNVERIFIED ❌]` | Score < 0.30 | Remover ou marcar explicitamente |
| `[CONTRADICTED 🚫]` | `contradictions` não-vazio | Afirmação contradiz fonte |

---

## ⚠️ ERROS COMUNS

| Erro | Causa | Solução |
|---|---|---|
| `confidence_check` retorna score baixo para fato óbvio | Fonte não disponível ou URL 404 | Fornecer URL alternativa (Wikipedia, docs oficiais) |
| Loop de auditoria trava em > 3 iterações | Claim não verificável por natureza | Marcar como `[INFERRED]` e documentar limitação |
| Budget esgotado durante auditoria | Relatório muito longo com muitas claims | Usar `TokenEconomy.smart_truncate()` antes de processar |
| `hallucination_flags` retorna flags mas score é alto | Evidência contraditória nas fontes | Verificar a contradição manualmente e escolher fonte primária oficial |

---

## 📋 HISTÓRICO

| Versão | Data | Mudanças |
|---|---|---|
| v1.0 | 2026-06-30 | Criação — Pipeline 5 passos + ResearchAuditor + TokenEconomy + Labels de Certificação |
