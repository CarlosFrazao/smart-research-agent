# ÍNDICE DE MISSÕES — PLANO SRA PARTE 3

> Guia rápido para o Claude Code executar o Plano Parte 3.
> Cada fase é um arquivo separado — carregue SOMENTE o arquivo da fase atual.
> **Não leia todas as fases de uma vez** — isso desperdiça context window sem necessidade.
> Fonte: `E:\Meus LLMs\Conversa\PLANO_SRA_PARTE_3.md`

---

## 📋 STATUS DAS FASES

| Fase | Arquivo da Missão | Prioridade | Pré-req | Status |
|---|---|---|---|---|
| **Fase 1** | `MISSAO_PARTE3_FASE1_ARQUITETURA_CRITICA.md` | 🔴 CRÍTICA | Parte 2 completa | ✅ Concluída |
| **Fase 2** | `MISSAO_PARTE3_FASE2_MODELO_DADOS_TRUST.md` | 🔴 CRÍTICA | Fase 1 | ⏳ Pendente |
| **Fase 3** | `MISSAO_PARTE3_FASE3_GENERIC_API_SEARCHER.md` | 🟢 Alta (maior alavancagem) | Fases 1-2 | ⏳ Pendente |
| **Fase 4** | `MISSAO_PARTE3_FASE4_CLUSTERING_CUSTO.md` | 🟠 Alta | Fases 1-3 | ⏳ Pendente |
| **Fase 5** | `MISSAO_PARTE3_FASE5_UI_ALLOWLIST_TRANSPARENCIA.md` | 🟡 Média | Fases 1-4 | ⏳ Pendente |

---

## 🛠️ SKILLS SELECIONADAS (em `E:\Meus LLMs\.claude\skills\`)

Todas as skills abaixo já existem no diretório — o Claude pode carregá-las diretamente.

| Skill | Caminho completo (absoluto) | Fases que usa |
|-------|----------------------------|--------------|
| **python-pro** | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | 1, 2, 3, 4, 5 |
| **systematic-debugging** | `E:\Meus LLMs\.claude\skills\systematic-debugging\SKILL.md` | 1 |
| **api-patterns** | `E:\Meus LLMs\.claude\skills\api-patterns\SKILL.md` | 1, 5 |
| **security-hardening** | `E:\Meus LLMs\.claude\skills\security-hardening\SKILL.md` | 1 |
| **test-driven-development** | `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md` | 2, 3, 4 |
| **clean-code** | `E:\Meus LLMs\.claude\skills\clean-code\SKILL.md` | 1, 2, 4 |
| **http-request-mastery** | `E:\Meus LLMs\.claude\skills\http-request-mastery\SKILL.md` | 3 |
| **ui-ux-pro-max** | `E:\Meus LLMs\.claire\skills\ui-ux-pro-max\SKILL.md` | 5 |

---

## 📦 RESUMO DAS FASES (para rápida orientação)

### Fase 1 — Correções Críticas de Arquitetura
Conecta 3 subsistemas órfãos (`enable_auditor`, `ReActOrchestrator`, modo `"debate"` na UI) e resolve a divergência crítica entre dois servidores FastAPI (`api/main.py` vs `src/mcp_server.py`). Remove diretório de código morto (`src/search/common/`). **Sem essa fase, as outras não valem nada.**

### Fase 2 — Modelo de Dados + TrustRuleStore
Adiciona 3 campos novos em `SearchResult` (`cluster_id`, `corroborated_by`, `trust_tier`) e cria `TrustRuleStore` (mirror do `FeedbackStore`). Integra regras de allowlist no `SourcePlanner`. Tudo aditivo — zero breaking changes.

### Fase 3 — GenericAPISearcher + Fontes Verticais
Cria `GenericAPISearcher` (configurável via YAML, sem Python novo por fonte), `config/generic_sources.yaml` com 8 fontes reais (Wikipedia, OpenLibrary, NPM, PyPI, Dictionary, MusicBrainz, WHOIS, Open-Meteo), e testes de conformidade com fixtures. Registra no `SearcherFactory`.

### Fase 4 — Clustering de Resultados + Estimativa de Custo
Implementa `cluster_similar_results()` reutilizando embeddings já calculados pelo `HybridRanker`. Atualiza `ScoreStage` e `ReportStage` para tratar clusters como unidade. Adiciona estimativa de custo pré-busca e endpoint `?dry_run=true`.

### Fase 5 — UI Allowlist/Denylist + Detecção de Query Vaga
Move heurística de query vaga para dentro do `IntentAnalyzer` (HITL pré-pipeline). UI de allowlist/denylist no sidebar do Streamlit. Painel de transparência de busca (fontes tentadas, confiança por claim, custo estimado). Integra `trust_tier` no `SearchStage`/`ScoreStage`.

---

## 🚫 DELIBERADAMENTE FORA DO PLANO PARTE 3

| Item | Motivo da exclusão |
|------|--------------------|
| `GenericWebsiteSearcher` | Requer decisão de produto sobre governance de crawling |
| Persistência de allowlist entre sessões | Depende de autenticação real (Parte 2 §9.1) |
| Dados financeiros em tempo real | APIs pagas, decisão de orçamento |
| Placar esportivo ao vivo | APIs comerciais, decisão de orçamento |
| Genealogia/registros públicos de pessoas | Risco de abuso — exige auth robusta antes de qualquer implementação |
| Paginação no `GenericAPISearcher` | Limitação conhecida v1 — implementar só quando caso de uso real exigir |

---

## 📜 PROTOCOLO DE EXECUÇÃO

1. **Abrir SOMENTE o arquivo da fase atual** — não ler as outras fases
2. **Ler o arquivo de missão INTEIRO** antes de escrever qualquer código
3. **Carregar as skills listadas** para a fase atual antes de codificar
4. **Executar as tarefas na ordem indicada** (há dependências entre elas)
5. **Rodar pytest ao final de cada tarefa** (não só ao final da fase)
6. **Fazer commit** ao completar todos os critérios da fase
7. **Atualizar este ÍNDICE** marcando a fase como `✅ Concluída (commit ...)`
8. **Atualizar o `CLAUDE.md`** com o novo status antes de iniciar a próxima fase
