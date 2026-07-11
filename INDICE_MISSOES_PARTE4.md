# ÍNDICE DE MISSÕES — PLANO SRA PARTE 4

> Guia rápido para o Claude Code executar o Plano Parte 4.
> Cada fase é um arquivo separado — carregue SOMENTE o arquivo da fase atual.
> **Não leia todas as fases de uma vez** — isso desperdiça context window sem necessidade.
> Fonte: `E:\Meus LLMs\Conversa\PLANO_SRA_PARTE_4.md`

---

## 🎯 OBJETIVO DO PLANO PARTE 4

Transformar o SRA de uma ferramenta de pesquisa técnica/dev em um **canivete suíço de pesquisa geral** capaz de:
- Monitorar notícias e eventos do mundo real em tempo real (GDELT, Google News, NewsAPI, Bluesky, Mastodon).
- Priorizar conteúdo realmente recente (correção do campo `published_at` no ranking).
- Detectar linhagem de citação (origem real de uma notícia vs. reproduções em massa).
- Combater viés de confirmação com passada adversarial embutida no pipeline.
- Monitorar tópicos continuamente com as tools MCP `monitor_topic` e `get_trending`.
- Entregar um briefing diário pronto para consumo por IAs e usuários.

---

## 📋 STATUS DAS FASES

| Fase | Arquivo da Missão | Prioridade | Pré-req | Status |
|---|---|---|---|---|
| **Fase 1** | `MISSAO_PARTE4_FASE1_FRESHNESS.md` | 🔴 CRÍTICA (bloqueante) | Plano Parte 3 completo | ⏳ Pendente |
| **Fase 2** | `MISSAO_PARTE4_FASE2_FONTES_NOTICIAS.md` | 🔴 CRÍTICA | Fase 1 | ⏳ Pendente |
| **Fase 3** | `MISSAO_PARTE4_FASE3_LINEAGEM_ADVERSARIAL.md` | 🟠 Alta | Fases 1 e 2 | ⏳ Pendente |
| **Fase 4** | `MISSAO_PARTE4_FASE4_MONITOR_TRENDING.md` | 🟠 Alta | Fases 1 e 2 | ⏳ Pendente |
| **Fase 5** | `MISSAO_PARTE4_FASE5_ROTEAMENTO_POLIMENTO.md` | 🟡 Média (polimento final) | Fases 1-4 | ⏳ Pendente |

---

## 🛠️ SKILLS SELECIONADAS (em `E:\Meus LLMs\.claude\skills\`)

| Skill | Caminho completo (absoluto) | Fases que usa |
|-------|----------------------------|--------------| 
| **python-pro** | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | 1, 2, 3, 4, 5 |
| **test-driven-development** | `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md` | 1, 2, 3, 4, 5 |
| **clean-code** | `E:\Meus LLMs\.claude\skills\clean-code\SKILL.md` | 1, 3, 5 |
| **http-request-mastery** | `E:\Meus LLMs\.claude\skills\http-request-mastery\SKILL.md` | 2 |
| **web-scraping-resilience** | `E:\Meus LLMs\.claude\skills\web-scraping-resilience\SKILL.md` | 2 |
| **mcp-server-development** | `E:\Meus LLMs\.claude\skills\mcp-server-development\SKILL.md` | 4 |
| **api-patterns** | `E:\Meus LLMs\.claude\skills\api-patterns\SKILL.md` | 4 |
| **prompt-engineering** | `E:\Meus LLMs\.claude\skills\prompt-engineering\SKILL.md` | 3 |
| **ui-ux-pro-max** | `E:\Meus LLMs\.claude\skills\ui-ux-pro-max\SKILL.md` | 5 |

---

## 📦 RESUMO DAS FASES

### Fase 1 — Corrigir o Freshness (`published_at` ausente) — BLOQUEANTE
Adicionar `SearchResult.published_at` em `types.py`. Corrigir `_compute_freshness()` no `HybridRanker` para usar a data real de publicação (com fallback para `fetched_at`). Adicionar meia-vidas específicas para fontes de notícia (12h para GDELT, 6h para redes sociais). Popular `published_at` nos searchers existentes que já têm essa data (RSS, HackerNews, Reddit). **Sem esta fase, nenhuma fonte de notícia consegue priorizar o mais recente.**

### Fase 2 — Fontes de Notícias Gerais
Adicionar GDELT, NewsAPI, Google News RSS (via novo `GenericFeedSearcher`), Bluesky e Mastodon sem escrever classes Python novas por fonte — apenas configuração YAML declarativa. Registrar todos no `SearcherFactory`. Popular `published_at` nos novos searchers.

### Fase 3 — Linhagem de Citação + Passada Adversarial + Seção de Confiança
Criar `LineageStage` que detecta se um resultado é fonte primária ou reprodução (heurística de URL + data, sem LLM). Criar `AdversarialPassStage` que gera uma query oposta para combater viés de confirmação nos modos de profundidade alta. Adicionar seção `⚠️ Nível de Confiança por Afirmação` no relatório final.

### Fase 4 — Tools MCP `monitor_topic` + `get_trending` + Briefing Diário
Criar `MonitorStore` (persistência JSONL). Reconectar `scheduler.py`. Implementar tool MCP `monitor_topic` (vigília contínua) e `get_trending` (GDELT trending sem query específica). Criar endpoint REST `GET /api/v1/briefing/latest` com job agendado diário.

### Fase 5 — Roteamento Universal + Multi-perspectiva + Polimento Final
Adicionar domínios `universal` e `news` no `domains.yaml` com fontes de notícia como `primary`. Atualizar `SourcePlanner` para detectar e rotear automaticamente queries gerais. Renderizar multi-perspectiva usando o campo `tone` do GDELT no relatório e na UI Streamlit.

---

## 🚫 DELIBERADAMENTE FORA DO PLANO PARTE 4

| Item | Motivo da exclusão |
|------|--------------------| 
| X/Twitter | API paga — custo e dependência comercial injustificáveis para o benefício |
| Dados financeiros em tempo real | APIs pagas, decisão de orçamento |
| Contornar paywall/robots.txt | Risco de ban de IP/chave, violação ética — fora por design |
| Rastreamento de pessoas físicas | Risco de abuso — exige auth robusta antes |
| Autenticação real na API | Projeto separado (Auditoria Parte 2 §9.1) |

---

## 📜 PROTOCOLO DE EXECUÇÃO

1. **Abrir SOMENTE o arquivo da fase atual** — não ler as outras fases.
2. **Ler o arquivo de missão INTEIRO** antes de escrever qualquer código.
3. **Carregar as skills listadas** para a fase atual antes de codificar.
4. **Executar as tarefas na ordem indicada** (há dependências entre elas).
5. **Rodar pytest ao final de cada tarefa** (não só ao final da fase).
6. **Fazer commit** ao completar todos os critérios da fase.
7. **Atualizar este ÍNDICE** marcando a fase como `✅ Concluída (commit ...)`.
8. **Atualizar o `CLAUDE.md`** com o novo status antes de iniciar a próxima fase.
9. **NÃO tentar dar push no repositório raiz** (`gemini-cli`) — push apenas no repositório do SRA (`CarlosFrazao/smart-research-agent`).
