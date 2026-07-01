# SMART RESEARCH AGENT — GUIA DE IMPLEMENTACAO PARA O CLAUDE

## Codinome: TechCurator | Versao: 1.0 | Data: 2026-06-16

---

## ANTES DE COMECAR — LEIA ISSO

Este projeto e dividido em **4 partes**, cada uma com **5 etapas** (total: 20
etapas). **NAO implemente tudo de uma vez.** Siga etapa por etapa, valide cada
uma antes de prosseguir.

---

## ESTRUTURA DAS PARTES

| Parte | Arquivo                                                       | Etapas | Foco                                                               | Tamanho      |
| ----- | ------------------------------------------------------------- | ------ | ------------------------------------------------------------------ | ------------ |
| **1** | `E:\Meus LLMs\smart-research-agent\PARTE_1_FUNDACAO.md`       | 1-5    | Estrutura, config, types, utilitarios, cache                       | 629 linhas   |
| **2** | `E:\Meus LLMs\smart-research-agent\PARTE_2_CLIENTS_SEARCH.md` | 6-10   | LLM Client, Firecrawl, todos os searchers                          | 758 linhas   |
| **3** | `E:\Meus LLMs\smart-research-agent\PARTE_3_INTELIGENCIA.md`   | 11-15  | Intent Analyzer, Query Expander, Ranker, Gap Detector, Synthesizer | 858 linhas   |
| **4** | `E:\Meus LLMs\smart-research-agent\PARTE_4_ORQUESTRACAO.md`   | 16-20  | Report Generator, Orchestrator, CLI, Docker, Testes E2E            | 1.018 linhas |

---

## HABILIDADES REQUERIDAS (SKILLS)

Para executar as tarefas descritas neste guia, o Claude CLI usará as seguintes
habilidades locais:

- [clean-code](file:///E:/Meus LLMs/.claude/skills/clean-code/SKILL.md)
- [docker-expert](file:///E:/Meus LLMs/.claude/skills/docker-expert/SKILL.md)
- [firecrawl-extractor](file:///E:/Meus
  LLMs/.claude/skills/firecrawl-extractor/SKILL.md)
- [http-request-mastery](file:///E:/Meus
  LLMs/.claude/skills/http-request-mastery/SKILL.md)
- [prompt-engineering](file:///E:/Meus
  LLMs/.claude/skills/prompt-engineering/SKILL.md)
- [python-patterns](file:///E:/Meus
  LLMs/.claude/skills/python-patterns/SKILL.md)
- [python-pro](file:///E:/Meus LLMs/.claude/skills/python-pro/SKILL.md)
- [test-driven-development](file:///E:/Meus
  LLMs/.claude/skills/test-driven-development/SKILL.md)
- [api-patterns](file:///E:/Meus LLMs/.claude/skills/api-patterns/SKILL.md)
- [web-scraping-resilience](file:///E:/Meus
  LLMs/.claude/skills/web-scraping-resilience/SKILL.md)
- [multi-agent-patterns](file:///E:/Meus
  LLMs/.claude/skills/multi-agent-patterns/SKILL.md)
- [local-llm-orchestrator](file:///E:/Meus
  LLMs/.claude/skills/local-llm-orchestrator/SKILL.md)
- [agent-evaluation](file:///E:/Meus
  LLMs/.claude/skills/agent-evaluation/SKILL.md)

---

## FLUXO DE IMPLEMENTACAO

```
Parte 1 (Fundacao)
  ├── Etapa 1: Criar estrutura de diretorios
  ├── Etapa 2: pyproject.toml, .env.example, config.py, Makefile
  ├── Etapa 3: types.py (dataclasses)
  ├── Etapa 4: HTTP Client, Query Cleaner, Logger
  └── Etapa 5: Cache, Deduplicator
       ↓ [VALIDAR: imports funcionam?]
Parte 2 (Clients e Search)
  ├── Etapa 6: LLM Client (5 providers)
  ├── Etapa 7: Firecrawl Client
  ├── Etapa 8: GitHub Searcher + Base Searcher
  ├── Etapa 9: Reddit + HN Searchers
  └── Etapa 10: Awesome + Arxiv + ProductHunt + Web + Firecrawl Searchers
       ↓ [VALIDAR: pytest passa com mocks?]
Parte 3 (Inteligencia)
  ├── Etapa 11: Intent Analyzer
  ├── Etapa 12: Query Expander
  ├── Etapa 13: Source Planner
  ├── Etapa 14: Quality Ranker
  └── Etapa 15: Gap Detector + Synthesizer
       ↓ [VALIDAR: testes de integracao passam?]
Parte 4 (Orquestracao)
  ├── Etapa 16: Report Generator
  ├── Etapa 17: Orchestrator
  ├── Etapa 18: CLI (main.py)
  ├── Etapa 19: Docker + Documentacao
  └── Etapa 20: Testes E2E + Benchmark
       ↓ [VALIDAR: 5 queries de benchmark passam?]
PROJETO COMPLETO
```

---

## REGRAS DE OURO PARA IMPLEMENTACAO

### 1. Copie, adapte, melhore — nao reinvente

Cada etapa indica **exatamente qual arquivo** copiar de cada repositorio base.
Use o codigo fornecido como ponto de partida, nao como texto sagrado. Se voce
achar que pode fazer melhor, faca.

### 2. Teste a cada etapa

Cada etapa tem um **criterio de aceitacao**. Nao passe para a proxima ate ele
estar verde.

### 3. Use o cache desde o inicio

O cache (`E:\Meus LLMs\smart-research-agent\src\utils\cache.py`) ja esta
implementado na Etapa 5. Use-o em todos os searchers para evitar re-pesquisas
caras durante desenvolvimento.

### 4. LLM Agnostico

O sistema deve funcionar com **qualquer** provider: Anthropic, OpenAI, Gemini,
OpenRouter, Ollama. Nao hardcode nenhum.

### 5. Fallbacks sao obrigatorios

Todo searcher deve ter `fallback()`. Se a API falhar, o sistema nao quebra.

---

## REPOSITORIOS BASE (links diretos)

| Repositorio            | URL                                                   | O que extrair                                                                                                       |
| ---------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| **Tino AI**            | https://github.com/melgarafael/tino-ai                | `lib/fetch.mjs`, `lib/rank-mock.mjs`, estrutura de prompts, organizacao de pastas                                   |
| **Last30Days**         | https://github.com/mvanhorn/last30days-skill          | `skills/last30days/scripts/lib/`, resilient search, timeout budgets, cross-source merge, per-author cap             |
| **Firecrawl**          | https://github.com/firecrawl/firecrawl                | `apps/python-sdk/`, endpoints scrape/search/crawl/map                                                               |
| **GPT Researcher**     | https://github.com/assafelovic/gpt-researcher         | `gpt_researcher/utils/llm.py`, `actions/`, `report_type/`, planner/subqueries                                       |
| **Open Deep Research** | https://github.com/langchain-ai/open_deep_research    | `E:\Meus LLMs\smart-research-agent\src/open_deep_research/deep_researcher.py`, brief generation, supervisor pattern |
| **DeepSearcher**       | https://github.com/zilliztech/deep-searcher           | Evaluation loop, reasoning-based search, multi-LLM config                                                           |
| **LangGraph Local**    | https://github.com/langchain-ai/local-deep-researcher | Configuracao Ollama/local, prompts                                                                                  |
| **Firecrawl Docs**     | https://github.com/firecrawl/firecrawl-docs           | Referencia de API, exemplos                                                                                         |

---

## CHECKLIST DE VALIDACAO POR PARTE

### Parte 1 — Fundacao

- [ ] `python -c "from src.types import *; from src.config import Config; print('OK')"`
      funciona
- [ ] `pytest E:\Meus LLMs\smart-research-agent\tests/` nao quebra (mesmo vazio)
- [ ] `make install` instala o projeto
- [ ] Cache persiste e recupera dados
- [ ] Deduplicator remove duplicatas corretamente

### Parte 2 — Clients e Search

- [ ] `pytest E:\Meus LLMs\smart-research-agent\tests/test_searchers/` passa com
      mocks
- [ ] Cada searcher herda de `BaseSearcher`
- [ ] Cada searcher tem `fallback()` funcionando
- [ ] LLM Client funciona com pelo menos 3 providers
- [ ] Firecrawl Client conecta (ou tem fallback graceful)

### Parte 3 — Inteligencia

- [ ] IntentAnalyzer classifica corretamente 5 queries de teste
- [ ] QueryExpander gera 8-12 variacoes
- [ ] SourcePlanner mapeia dominio -> fontes corretamente
- [ ] Ranker produz scores entre 0-100
- [ ] GapDetector identifica quando faltam fontes
- [ ] Synthesizer deduplica e clusteriza corretamente

### Parte 4 — Orquestracao

- [ ] `python E:\Meus LLMs\smart-research-agent\src\main.py version` funciona
- [ ] `python E:\Meus LLMs\smart-research-agent\src\main.py research \"test query\"`
      gera relatorio valido
- [ ] Relatorio tem todas as 8 secoes
- [ ] `docker build` constroi com sucesso
- [ ] Servidor MCP local (SSE no Docker e stdio no host via ponte
      `mcp-server.mjs`) respondendo e integrado
- [ ] 5 queries de benchmark passam (mesmo que lentamente)

---

## ARQUITETURA FINAL ESPERADA

```
E:\Meus LLMs\smart-research-agent/
├── E:\Meus LLMs\smart-research-agent\src/
│   ├── main.py              # CLI
│   ├── orchestrator.py      # Coordena pipeline
│   ├── config.py            # Configuracoes
│   ├── types.py             # Dataclasses
│   ├── intent_analyzer.py   # Classifica dominio
│   ├── query_expander.py    # Expande queries
│   ├── source_planner.py    # Escolhe fontes
│   ├── ranker.py            # Score 0-100
│   ├── gap_detector.py      # Detecta lacunas
│   ├── synthesizer.py       # Consolida resultados
│   ├── report_generator.py  # Gera Markdown
│   ├── mcp_server.py        # Servidor MCP expondo as tools de pesquisa (FastAPI/SSE)
│   ├── search/
│   │   ├── base_searcher.py
│   │   ├── github_searcher.py
│   │   ├── reddit_searcher.py
│   │   ├── hn_searcher.py
│   │   ├── awesome_searcher.py
│   │   ├── arxiv_searcher.py
│   │   ├── producthunt_searcher.py
│   │   ├── web_searcher.py
│   │   └── firecrawl_searcher.py
│   ├── clients/
│   │   ├── llm_client.py      # 5 providers
│   │   └── firecrawl_client.py
│   └── utils/
│       ├── http_client.py
│       ├── cache.py
│       ├── deduplicator.py
│       ├── query_cleaner.py
│       └── logger.py
├── E:\Meus LLMs\smart-research-agent\prompts/               # 7 prompts do sistema
├── E:\Meus LLMs\smart-research-agent\config/                # 3 arquivos YAML
├── E:\Meus LLMs\smart-research-agent\tests/                 # Unit + E2E
├── E:\Meus LLMs\smart-research-agent\docs/                  # Arquitetura + API
├── E:\Meus LLMs\smart-research-agent\mcp-server.mjs         # Ponte STDIO-to-SSE para conexão local à IDE
├── E:\Meus LLMs\smart-research-agent\docker-compose.yml
└── E:\Meus LLMs\smart-research-agent\Dockerfile
```

---

## PIPELINE DE EXECUCAO

```
Usuario: "CRM open source parecido com HubSpot"
         |
         v
+---------------------+
| 1. Intent Analyzer  | --> Dominio: saas_b2b, Entidades: [HubSpot, CRM]
+---------------------+
         |
         v
+---------------------+
| 2. Query Expander   | --> 10 variacoes (CRM AI, open source CRM, etc.)
+---------------------+
         |
         v
+---------------------+
| 3. Source Planner   | --> Primarias: GitHub, Reddit, ProductHunt
+---------------------+
         |
         v
+-----------------------------------------+
| 4. Parallel Search                      |
|    |-- GitHub (repos)                   |
|    |-- Reddit (discussoes)              |
|    |-- HN (threads)                     |
|    |-- Awesome Lists (curadoria)        |
|    |-- Product Hunt (lancamentos)       |
|    |-- Firecrawl (web)                  |
+-----------------------------------------+
         |
         v
+---------------------+
| 5. Quality Rank     | --> Score 0-100 por fonte
+---------------------+
         |
         v
+---------------------+
| 6. Gap Detector     | --> Faltou algo? -> Volta ao passo 3
+---------------------+
         |
         v
+---------------------+
| 7. Synthesizer      | --> Deduplica, clusteriza, merge
+---------------------+
         |
         v
+---------------------+
| 8. Report Generator | --> Markdown com 8 secoes
+---------------------+
         |
         v
    Relatorio Final (.md)
```

---

## TAMANHO ALVO DO PROJETO

| Metrica          | Alvo                    |
| ---------------- | ----------------------- |
| Arquivos Python  | ~25                     |
| Linhas de codigo | ~3.500                  |
| Prompts          | 7                       |
| Configs YAML     | 3                       |
| Testes           | Unit + E2E + Benchmark  |
| Runtime          | Python 3.11+            |
| Container        | Docker + docker-compose |

---

## COMO COMECAR AGORA

1. **Leia a Parte 1 completa**
   (`E:\Meus LLMs\smart-research-agent\PARTE_1_FUNDACAO.md`)
2. **Implemente a Etapa 1** (estrutura de diretorios)
3. **Valide** com o criterio de aceitacao
4. **Prossiga para Etapa 2**
5. **Repita ate a Etapa 20**

---

## DICAS PARA O CLAUDE

- **Se um LLM provider nao estiver disponivel**, use outro. O sistema e
  agnostico.
- **Se uma API externa falhar**, o fallback deve funcionar. Nao deixe o sistema
  quebrar.
- **Se o cache nao estiver funcionando**, re-pesquisas serao lentas e caras.
  Conserte antes de continuar.
- **Se o deduplicator nao estiver funcionando**, o relatorio tera repeticao.
  Teste com queries reais.
- **Use `pytest -x`** para parar no primeiro erro e consertar antes de
  continuar.

---

## CONTATO E SUPORTE

Este e um projeto open source (MIT). Para duvidas:

- Consulte os repositorios base listados acima
- Verifique os prompts em `E:\Meus LLMs\smart-research-agent\prompts/` para
  entender a logica de cada modulo
- Execute `python -m src.main research --help` para ver opcoes da CLI

---

**Boa implementacao! 🚀**

_Documento gerado em 2026-06-16. Smart Research Agent v1.0._
