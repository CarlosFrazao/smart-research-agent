# ÍNDICE DE MISSÕES — AUDITORIA SRA PARTE 2

> Guia rápido para o Claude Code executar o plano derivado de `AUDITORIA_SRA_PARTE_2.md`.
> Cada fase é um arquivo separado — carregue SOMENTE o arquivo da fase atual.
> **Não leia todas as fases de uma vez** — isso desperdiça context window sem necessidade.

---

## 📋 STATUS DAS FASES

| Fase | Arquivo da Missão | Prioridade | Pré-req | Status |
|---|---|---|---|---|
| **Fase 1** | `MISSAO_PARTE2_FASE1_CONECTORES_E_FIACAO.md` | 🔴 CRÍTICA | Nenhum | ✅ Concluída (commit `ab9ca71`) |
| **Fase 2** | `MISSAO_PARTE2_FASE2_CONFIG_E_HITL.md` | 🟠 Alta | Fase 1 | ✅ Concluída (commit `5075b046b`) |
| **Fase 3** | `MISSAO_PARTE2_FASE3_SEGURANCA_API.md` | 🔴 CRÍTICA | Fase 1 | ✅ Concluída (commit `ebf8b61`) |
| **Fase 4** | `MISSAO_PARTE2_FASE4_FEEDBACK_RESULTID.md` | 🔴 CRÍTICA | Fase 1 | ✅ Concluída (commit `121bef3`) |
| **Fase 5** | `MISSAO_PARTE2_FASE5_INFRA_E_DIVIDA_TECNICA.md` | 🟡 Média | Fases 1-4 | ✅ Concluída (commit `5a1cd5a`) |
| **Fase 6** | `MISSAO_PARTE2_FASE6_UNIVERSAL_SEARCHER.md` | 🟢 Alta (novo valor) | Fases 1-5 | ✅ Concluída |

---

## 🛠️ SKILLS DISPONÍVEIS (em `.claude/skills/`)

Todas as skills abaixo estão em `.claude/skills/` e prontas para uso:

| Skill | Pasta | Usada nas Fases |
|---|---|---|
| `python-pro` | `.claude/skills/python-pro/SKILL.md` | 1, 2, 3, 4, 5, 6 |
| `test-driven-development` | `.claude/skills/test-driven-development/SKILL.md` | 1, 4, 6 |
| `clean-code` | `.claude/skills/clean-code/SKILL.md` | 2, 4, 5, 6 |
| `api-patterns` | `.claude/skills/api-patterns/SKILL.md` | 2, 3, 6 |
| `security-hardening` | `.claude/skills/security-hardening/SKILL.md` | 3 |

---

## 🚀 PROTOCOLO DE EXECUÇÃO

### Como o Claude deve começar cada fase:

1. Ler o arquivo `MISSAO_PARTE2_FASE{N}_*.md` completamente
2. Carregar as skills listadas no arquivo (ler o SKILL.md de cada uma)
3. Ler os arquivos do projeto alvo ANTES de escrever código
4. Executar as tarefas em ordem
5. Validar com os comandos de cada seção "Critério de Conclusão"
6. Atualizar este índice (mudar status de ⏳ para ✅)

### Ordem de execução recomendada:

```
Fase 1 → (Fase 2 + Fase 3 + Fase 4 em paralelo, pois não dependem entre si) → Fase 5 → Fase 6
```

> Fases 2, 3 e 4 podem ser executadas em paralelo pois não modificam os mesmos arquivos.
> Fases 5 e 6 devem aguardar as anteriores para ter a suíte de testes completa como net de segurança.

---

## 📐 RESUMO DOS ACHADOS POR FASE

### Fase 1 — Bloqueantes Críticos (Conectores + Wiring Test)
- Notion/Confluence/SharePoint são fontes primárias em 5/7 domínios mas **nunca executadas** (silenciosamente)
- Falta teste de integração que valida o wiring `SourcePlanner ↔ SearcherFactory`

### Fase 2 — Config Morta + HITL no-op + Exporters Órfãos
- `scoring_weights.yaml` e `sources.yaml` nunca são lidos (editar não tem efeito)
- Veto HITL só loga, não filtra resultados
- BibTeX/RIS existem e são testados, nunca chamados pelo `report_generator.py`
- `misinformation_domains.yaml` tem 4 domínios de placeholder

### Fase 3 — Segurança da API REST (CRÍTICA)
- Zero autenticação na API (qualquer IP pode disparar pesquisas pagas)
- CORS aberto com `allow_origins=["*"]` + bind em `0.0.0.0`
- CI sem `pip-audit` (scanning de vulnerabilidades de dependências)

### Fase 4 — Sistema de Feedback Quebrado (CRÍTICA) + ResearchAuditor órfão ⭐
- `POST /feedback` gera `result_id = sha1(query)`, `FeedbackRanker` usa `sha1(entity:title)` — nunca coincidem
- `FeedbackRanker` nunca instanciado em produção (só em testes)
- `SanitizationStage.run()` é literalmente `pass` (toda pesquisa executa etapa que não faz nada)
- `source_name` falta no `FeedbackStore.record()` (pré-requisito de dado para personalização)
- ⭐ **NOVO (§14.1):** `ResearchAuditor` — loop completo de fact-checking/verificação de claims, implementado com docstring de integração, instanciado em `stage_factory.py`, mas **nunca chamado** em nenhum stage. Maior impacto percebido pelo usuário de toda a auditoria.

### Fase 5 — Dívida Técnica de Infraestrutura
- `httpx`, `tiktoken`, `tenacity`, `redis` usados em 15+ arquivos mas não declarados em `pyproject.toml`
- 5 de 8 prompts em `prompts/` nunca carregados por código (mesma questão das configurações mortas)
- `react_orchestrator.py` + `decision_engine.py`: segunda arquitetura de orquestração nunca conectada

### Fase 6 — Canivete Suíço Universal (Novo Valor)
- `GenericAPISearcher`: adicionar fonte via YAML sem código Python novo
- MCP tool `search_anything`: expor o modo universal para outros agentes
- Promover `scheduler.py` (monitoramento contínuo) para CLI/API oficiais
- Parser de operadores avançados (`site:`, `filetype:`, `intitle:`)
