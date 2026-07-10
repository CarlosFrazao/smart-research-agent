# MISSÃO PARTE3 — FASE 5: UI Allowlist/Denylist + Detecção de Query Vaga no Pipeline

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 5 (e última) do plano derivado de `PLANO_SRA_PARTE_3.md`.
> Pré-requisito: **Todas as fases 1-4 concluídas**.
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — FECHANDO O CICLO DE USABILIDADE

Esta fase fecha o ciclo de valor para o usuário final:
1. **Detecção de query vaga** → o SRA agora pergunta ao usuário antes de gastar tokens numa query ambígua
2. **Allowlist/Denylist** → o usuário controla explicitamente quais fontes recebe (usando o `TrustRuleStore` criado na Fase 2)
3. **Painel de transparência** → o usuário entende de onde veio a resposta e por que confiar nela

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | Integração no pipeline Python |
| `api-patterns` | `E:\Meus LLMs\.claude\skills\api-patterns\SKILL.md` | Endpoint HITL e integração com `IntentAnalyzer` |
| `ui-ux-pro-max` | `E:\Meus LLMs\.claude\skills\ui-ux-pro-max\SKILL.md` | UI Streamlit: allowlist, transparência, painel de fontes |

---

## 📋 TAREFAS (em ordem recomendada)

### TAREFA 5.1 — Mover detecção de query vaga para dentro do `IntentAnalyzer`

**Contexto (§4.1):** `hooks/anti_query_vaga.py` já implementa a heurística (query < 15 chars, palavra isolada, stack trace sem pergunta). Hoje só roda como hook do Claude Code — não roda quando o SRA é acessado via API/MCP por outros agentes.

**O que fazer:**

1. Abrir `hooks/anti_query_vaga.py` e extrair a lógica de heurística (função pura, sem dependências do Claude Code).
2. Abrir `src/intent_analyzer.py` (ou `src/pipeline/stages/expand_stage.py`) e adicionar a verificação **antes** de gastar o pipeline:

```python
# Heurística de query vaga — dentro do IntentAnalyzer ou ExpandStage:
def _is_query_too_vague(query: str) -> bool:
    """Detecta queries que quase certamente não vão gerar resultado útil."""
    import re
    query = query.strip()
    if len(query) < 15:
        return True
    if len(query.split()) <= 2 and not any(c in query for c in "?!."):
        return True  # palavra(s) isolada(s) sem pontuação de pergunta
    # Stack trace colado sem pergunta:
    if re.search(r"(Traceback|Error:|Exception:)", query) and "?" not in query:
        return True
    return False
```

3. Se a query for vaga, acionar o HITL existente para pedir esclarecimento **antes** de iniciar o pipeline:

```python
# Dentro do IntentAnalyzer ou ExpandStage.run():
if _is_query_too_vague(context.query):
    # Usar mecanismo HITL existente (já implementado na Fase 2 da Parte 2)
    clarification = await self.hitl_handler.request_clarification(
        message=f"A query '{context.query}' parece muito genérica. "
                f"Pode dar mais contexto? Exemplo: em vez de 'python', "
                f"tente 'como fazer X em Python'.",
        session_id=context.session_id,
    )
    if clarification and clarification.refined_query:
        context.query = clarification.refined_query
        logger.info("Query refinada via HITL: %s → %s", context.query, clarification.refined_query)
    else:
        logger.info("Usuário não refinou a query — continuando com original")
```

> **Preservar o comportamento do hook original:** O hook de Claude Code (`hooks/anti_query_vaga.py`) pode continuar existindo — a nova versão no pipeline é adicional, não substituta.

**Validação:**
```bash
python -m py_compile src/intent_analyzer.py  # ou expand_stage.py
python -m pytest tests/ -k "intent or vague or query" -v
```

---

### TAREFA 5.2 — Integrar `trust_tier` no `SearchStage`/`ScoreStage`

**Contexto (§4.4):** O `TrustRuleStore` foi criado na Fase 2 e as regras já chegam em `context.extra["trust_rules"]`. O `SearchResult.trust_tier` foi adicionado na Fase 2. Agora falta **usar** esse campo no pipeline.

**O que fazer:**

1. Abrir `src/pipeline/stages/search_stage.py` e, após receber os resultados de cada searcher, preencher `result.trust_tier`:

```python
# Em SearchStage, após cada searcher retornar resultados:
trust_rules = context.extra.get("trust_rules", {})  # {source: "allow"|"deny"}
for result in results:
    tier = trust_rules.get(result.source, "neutral")
    result.trust_tier = tier
    if tier == "deny":
        logger.debug("Resultado de fonte negada: %s — excluindo", result.source)

# Filtrar resultados de fontes "deny" (configurável):
import os
filter_denied = os.environ.get("FILTER_DENIED_SOURCES", "true").lower() == "true"
if filter_denied:
    results = [r for r in results if r.trust_tier != "deny"]
```

2. Em `ScoreStage`, boostar score de resultados de fontes "allow":

```python
# Em ScoreStage, após calcular o score base:
ALLOW_BOOST = float(os.environ.get("ALLOW_SOURCE_SCORE_BOOST", "0.1"))  # configurável
for r in context.ranked_results:
    result = getattr(r, "result", r)
    if getattr(result, "trust_tier", "neutral") == "allow":
        if hasattr(r, "combined_score"):
            r.combined_score = min(1.0, r.combined_score + ALLOW_BOOST)
```

---

### TAREFA 5.3 — Implementar UI de allowlist/denylist no Streamlit

**Contexto (§10, §11.2):** O sidebar já tem seção "🧬 Recursos Avançados" com checkboxes. A allowlist se encaixa como nova seção no mesmo padrão visual.

**O que fazer:**

1. Abrir `ui/streamlit_app.py` e localizar o sidebar.
2. Adicionar a seção de "Fontes de Confiança" conforme o design do plano:

```python
# Em ui/streamlit_app.py, dentro do bloco with st.sidebar:
st.divider()
st.markdown("### 🎯 Fontes de Confiança")
with st.expander("Gerenciar regras de fonte", expanded=False):
    col_source, col_tier, col_add = st.columns([3, 2, 1])
    with col_source:
        new_source = st.text_input(
            "Fonte",
            placeholder="ex: reddit ou blog-duvidoso.com",
            label_visibility="collapsed",
            key="trust_new_source",
        )
    with col_tier:
        new_tier = st.selectbox(
            "Regra",
            options=["allow", "deny"],
            format_func=lambda x: "✅ Sempre priorizar" if x == "allow" else "🚫 Nunca mostrar",
            label_visibility="collapsed",
            key="trust_new_tier",
        )
    with col_add:
        if st.button("➕", use_container_width=True, key="trust_add_btn"):
            if new_source:
                rules = st.session_state.get("trust_rules", {})
                rules[new_source.strip().lower()] = new_tier
                st.session_state["trust_rules"] = rules
                st.rerun()

    # Tabela das regras ativas
    rules = st.session_state.get("trust_rules", {})
    if rules:
        st.markdown("**Regras ativas:**")
        for source, tier in list(rules.items()):
            c1, c2, c3 = st.columns([3, 2, 1])
            c1.write(f"`{source}`")
            c2.write("✅ Prioridade" if tier == "allow" else "🚫 Bloqueado")
            if c3.button("🗑️", key=f"del_rule_{source}"):
                del rules[source]
                st.session_state["trust_rules"] = rules
                st.rerun()
    else:
        st.caption("Nenhuma regra configurada — todas as fontes são tratadas igualmente.")
```

3. Passar as regras do `st.session_state` para o pipeline ao iniciar uma pesquisa:

```python
# Ao iniciar pesquisa no Streamlit:
context.extra["trust_rules"] = st.session_state.get("trust_rules", {})
```

> **Nota sobre persistência:** Por enquanto, as regras só duram a sessão do Streamlit (`st.session_state`). Quando a autenticação real existir (Parte 2 §9.1), integrar com `TrustRuleStore` para persistência entre visitas.

---

### TAREFA 5.4 — Painel de transparência de busca na UI

**Contexto (§4.3):** `evidence_quality`, `confidence_score`, dados de circuit breaker — tudo já existe internamente. Só falta uma camada de apresentação amigável.

**O que fazer:**

1. Após o relatório ser exibido no Streamlit, adicionar uma seção expansível de transparência:

```python
# Em ui/streamlit_app.py, após exibir o relatório:
with st.expander("🔍 Transparência da busca", expanded=False):
    st.markdown("**Fontes consultadas:**")

    # Fontes que contribuíram para o resultado
    sources_used = {}
    for result in results:  # context.ranked_results ou equivalente
        source = getattr(result, "source", "desconhecida")
        trust = getattr(result, "trust_tier", "neutral")
        if source not in sources_used:
            sources_used[source] = {"count": 0, "trust": trust, "avg_confidence": []}
        sources_used[source]["count"] += 1
        confidence = getattr(result, "confidence_score", None)
        if confidence:
            sources_used[source]["avg_confidence"].append(confidence)

    for source, info in sorted(sources_used.items(), key=lambda x: x[1]["count"], reverse=True):
        avg_conf = (sum(info["avg_confidence"]) / len(info["avg_confidence"])
                    if info["avg_confidence"] else None)
        trust_emoji = {"allow": "✅", "deny": "🚫", "neutral": "⚪"}.get(info["trust"], "⚪")
        conf_str = f"{avg_conf:.0%}" if avg_conf else "N/A"
        st.markdown(f"- {trust_emoji} **{source}**: {info['count']} resultado(s) | Confiança média: {conf_str}")

    # Custo estimado (se disponível)
    estimated_cost = context.extra.get("estimated_cost_usd")
    if estimated_cost:
        st.metric("Custo estimado", f"~${estimated_cost:.4f}")

    # Circuit breakers abertos (fontes que falharam)
    st.markdown("**Fontes com falha (circuit breaker):**")
    st.caption("Ver `/api/circuit-breakers` para detalhes completos.")
```

---

### TAREFA 5.5 — Testes e validação final

**O que fazer:**

```bash
# Verificar que não há regressões no pipeline completo:
python -m pytest tests/ --tb=short -q

# Verificar que o Streamlit compila:
python -m py_compile ui/streamlit_app.py

# Verificar que as integrações do trust_tier estão funcionando:
python -m pytest tests/ -k "trust or allowlist or denylist" -v

# Teste smoke do pipeline completo:
python -m cli.main "inteligência artificial aplicações" -m guerrilha -o /tmp/test_output.md
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 5 (e do PLANO PARTE 3 completo)

- [ ] `_is_query_too_vague()` integrada no `IntentAnalyzer`/`ExpandStage`
- [ ] HITL disparado para queries vagas antes de gastar o pipeline
- [ ] `SearchResult.trust_tier` preenchido no `SearchStage` a partir de `context.extra["trust_rules"]`
- [ ] Fontes "deny" filtradas ou penalizadas no `ScoreStage`
- [ ] Fontes "allow" boosted no `ScoreStage`
- [ ] UI de allowlist/denylist funcional no sidebar do Streamlit
- [ ] Regras salvas em `st.session_state` e passadas para o pipeline
- [ ] Painel de transparência mostrando fontes, confiança e custo estimado
- [ ] `python -m pytest tests/ --tb=short -q` → zero novas regressões
- [ ] `python -m py_compile ui/streamlit_app.py` → sem erros
- [ ] Commit com todos os arquivos desta fase
- [ ] **INDICE_MISSOES_PARTE3.md atualizado** com todas as fases como "Concluída"
- [ ] **CLAUDE.md atualizado** para refletir conclusão do Plano Parte 3

---

## 🚫 FORA DO ESCOPO DESTA FASE (e do Plano Parte 3 inteiro)

- `GenericWebsiteSearcher` → decisão de produto pendente (crawling governance)
- Persistência da allowlist entre sessões → depende de autenticação real (Parte 2 §9.1)
- Dados financeiros em tempo real (ações/cripto) → APIs pagas, decisão de orçamento
- Placar esportivo ao vivo → APIs comerciais, decisão de orçamento
- Genealogia/registros públicos de pessoas → fora de escopo (risco de abuso, requer auth robusta)
