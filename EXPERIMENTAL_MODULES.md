# Módulos Experimentais / WIP

Este arquivo documenta módulos que existem no repositório mas **não estão
conectados ao pipeline principal** (`Orchestrator` usado por `api/main.py`,
`cli/main.py` e `src/mcp_server.py`). São candidatos a integração futura ou
remoção. **Não são código morto** — são trabalho em andamento ou experimentos
intencionais, cobertos por testes.

> Sempre que um módulo aqui listado for integrado ao pipeline principal ou
> removido, atualize também a exceção correspondente em
> `tests/test_wiring_integration.py` (`KNOWN_EXPERIMENTAL_MODULES`).

---

## `src/react_orchestrator.py` + `src/decision_engine.py`

- **Status:** WIP experimental — arquitetura de orquestração dinâmica estilo
  ReAct, alternativa ao pipeline linear sequencial (`ResearchPipeline`).
- **Intenção:** Permitir que o orquestrador decida dinamicamente, a cada
  iteração, qual etapa executar em seguida (intent → expand → search → rank →
  verification → gap → synthesize → report), com base em confiança agregada,
  lacunas detectadas e métricas de avaliação. `ReActOrchestrator` é subclass de
  `Orchestrator` e mantém 100% de compatibilidade retroativa: quando
  `Config.enable_dynamic_loop=False` (default), o método `research()` do pai
  (pipeline clássico) é usado automaticamente. O `DynamicDecisionEngine`
  encapsula as regras de decisão explicáveis do loop.
- **Ponto de entrada previsto:** `Config.enable_dynamic_loop` (já declarado em
  `src/config.py`, default `False`). Nenhum ponto de produção instancia
  `ReActOrchestrator` hoje — a fábrica de orquestrador (`api`/`cli`/`mcp`) usa
  sempre o `Orchestrator` clássico.
- **Revisão prevista:** Auditoria Parte 2 — Fase 6 (decidir integração definitiva
  vs. remoção), ou quando houver demanda de produto por orquestração adaptativa.
- **Referência de testes:** `tests/test_react_orchestrator.py`,
  `tests/test_decision_engine.py`.

---

## Critérios para promover um módulo (sair desta lista)

Um módulo experimental deixa de ser experimental quando:

1. É instanciado por pelo menos um ponto de entrada de produção
   (`api/`, `cli/`, `src/mcp_server.py`).
2. Possui testes de integração (não apenas testes isolados de unidade).
3. Sua flag de ativação (quando houver) está documentada no README.

Ao satisfazer esses critérios, remova a entrada aqui e a exceção correspondente
em `tests/test_wiring_integration.py`.
