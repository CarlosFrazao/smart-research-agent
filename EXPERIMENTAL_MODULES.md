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

- **Status:** ✅ CONECTADO (Plano Parte 3 — Fase 1, §14.2) — arquitetura de
  orquestração dinâmica estilo ReAct, alternativa ao pipeline linear sequencial
  (`ResearchPipeline`). Deixou de ser órfão: a fábrica
  `src/orchestrator_factory.py::create_orchestrator` instancia
  `ReActOrchestrator` em todos os pontos de entrada (`api/main.py`,
  `cli/main.py`, `src/mcp_server.py`) quando `Config.enable_dynamic_loop=True`.
- **Intenção:** Permitir que o orquestrador decida dinamicamente, a cada
  iteração, qual etapa executar em seguida (intent → expand → search → rank →
  verification → gap → synthesize → report), com base em confiança agregada,
  lacunas detectadas e métricas de avaliação. `ReActOrchestrator` é subclass de
  `Orchestrator` e mantém 100% de compatibilidade retroativa: quando
  `Config.enable_dynamic_loop=False` (default), o método `research()` do pai
  (pipeline clássico) é usado automaticamente. O `DynamicDecisionEngine`
  encapsula as regras de decisão explicáveis do loop.
- **Ponto de entrada ativo:** `Config.enable_dynamic_loop` (declarado em
  `src/config.py`, default `False`). Quando `True`,
  `src/orchestrator_factory.py::create_orchestrator` retorna um
  `ReActOrchestrator` em vez do `Orchestrator` clássico — usado por
  `api/main.py`, `cli/main.py` e `src/mcp_server.py::get_orchestrator`.
- **Revisão concluída:** Plano Parte 3 — Fase 1 (integração definitiva via
  fábrica de orquestrador). Módulo agora satisfaz o critério 1 de promoção.
- **Referência de testes:** `tests/test_react_orchestrator.py`,
  `tests/test_decision_engine.py`.

---

## `api/main.py` (servidor REST legado)

- **Status:** 🟡 LEGADO / ALTERNATIVO (Plano Parte 3 — Fase 1, §15.2). Não é
  código morto: continua funcional para uso standalone da API REST e é a fonte
  das rotas REST reutilizadas pelo servidor oficial.
- **Contexto:** o projeto tinha dois `FastAPI()` divergentes — `api/main.py`
  (documentado no README antigo) e `src/mcp_server.py` (o que o Dockerfile
  sobe). O servidor **oficial** é `src/mcp_server.py`.
- **Unificação aplicada:** as rotas exclusivas de `api/main.py` (pesquisa
  síncrona/async com polling, streaming SSE, agendamento e observabilidade)
  foram extraídas para o `APIRouter` `rest_router`, que `src/mcp_server.py`
  inclui sob o prefixo `/api/v2`. As defesas de segurança (auth `X-API-Key`,
  CORS por env, rate limiting slowapi) foram aplicadas também no servidor
  oficial.
- **Referência de testes:** `tests/test_security_fase3.py` (app legado),
  `tests/test_mcp_server_v2.py::test_rest_router_mounted_under_api_v2` e
  `::test_research_endpoint_returns_500_on_error` (servidor oficial).

---

## Critérios para promover um módulo (sair desta lista)

Um módulo experimental deixa de ser experimental quando:

1. É instanciado por pelo menos um ponto de entrada de produção
   (`api/`, `cli/`, `src/mcp_server.py`).
2. Possui testes de integração (não apenas testes isolados de unidade).
3. Sua flag de ativação (quando houver) está documentada no README.

Ao satisfazer esses critérios, remova a entrada aqui e a exceção correspondente
em `tests/test_wiring_integration.py`.
