# MISSÃO PARTE4 — FASE 5: Roteamento Universal + Multi-perspectiva + Polimento Final

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Pré-requisito: **Todas as Fases 1-4 concluídas**.
> Esta é a fase final do Plano Parte 4.
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — EVITANDO VALIDATIONERRORS E INTEGRANDO APRESENTAÇÃO PREMIUM

Para que o roteador dinâmico funcione corretamente, o `IntentAnalyzer` e o `SourcePlanner` dependem da classe `Domain` definida no Pydantic. Se os domínios `universal` e `news` não forem cadastrados no Enum oficial `Domain`, o Pydantic levantará um `ValidationError` de forma silenciosa ou causará crashes, ativando fallbacks que impedem o uso das fontes de notícias gerais.

Esta fase:
1. Cadastra os novos domínios no Enum `Domain` de `src/types.py`.
2. Cria os mapeamentos de fontes no `domains.yaml`.
3. Atualiza o detector de intenções do `SourcePlanner` para queries gerais.
4. Apresenta perspectivas críticas e positivas em relatórios usando o `tone` do GDELT.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | Alterações em types.py, source_planner.py e report_generator.py |
| `ui-ux-pro-max` | `E:\Meus LLMs\.claude\skills\ui-ux-pro-max\SKILL.md` | Atualizar UI Streamlit com badges de sentimentos/tom |
| `clean-code` | `E:\Meus LLMs\.claude\skills\clean-code\SKILL.md` | Garantir backward compat nos enums e mapeamentos |
| `test-driven-development` | `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md` | Testes de roteamento de queries de notícias |

---

## 📋 TAREFAS (em ordem obrigatória)

### TAREFA 5.1 — Registrar domínios no Enum `Domain` em `src/types.py`

**Arquivo:** `src/types.py` (por volta da linha 119)

Adicionar as entradas `UNIVERSAL` e `NEWS`:

```python
class Domain(StrEnum):
    """Domínios de pesquisa suportados pelo Smart Research Agent."""

    SAAS_B2B = "saas_b2b"
    DEV_TOOLS = "dev_tools"
    AI_ML = "ai_ml"
    AUTOMATION = "automation"
    INFRASTRUCTURE = "infrastructure"
    OPEN_SOURCE = "open_source"
    GENERAL = "general"
    UNIVERSAL = "universal"  # Novo: pesquisa sem viés técnico
    NEWS = "news"            # Novo: notícias e eventos atuais
```

Rode a compilação rápida para validar a sintaxe:
```bash
python -m py_compile src/types.py
```

---

### TAREFA 5.2 — Adicionar domínios `universal` e `news` no `domains.yaml`

**Arquivo:** `config/domains.yaml`

Adicionar as regras de prioridade para as novas fontes integradas na Fase 2:

```yaml
  universal:
    description: "Pesquisa geral sem viés técnico — notícias, mundo, política, economia, cultura"
    primary_sources:
      - gdelt
      - google_news_rss
      - newsapi_org
      - bluesky
      - mastodon_social
      - duckduckgo
    secondary_sources:
      - reddit
      - hackernews
    disabled_sources:
      - github
      - arxiv
      - pypi

  news:
    description: "Notícias e cobertura jornalística — eventos atuais, breaking news"
    primary_sources:
      - gdelt
      - google_news_rss
      - newsapi_org
    secondary_sources:
      - bluesky
      - mastodon_social
      - reddit
    disabled_sources:
      - github
      - arxiv
```

---

### TAREFA 5.3 — Roteamento dinâmico de queries de notícias no `SourcePlanner`

**Arquivo:** `src/source_planner.py`

Atualizar o método de detecção ou de fallback de domínio para rotear queries gerais/noticiosas para os novos domínios em vez de cair na tabela de infra/dev.

```python
# Adicionar no topo do arquivo ou dentro do SourcePlanner:
NEWS_KEYWORDS = ["notícia", "noticias", "aconteceu", "hoje", "semana", "eleição", "governo",
                 "economia", "esporte", "guerra", "política", "atualidade", "mundo",
                 "breaking", "news", "today", "happening"]

TECH_KEYWORDS = ["python", "api", "github", "npm", "docker", "framework",
                 "library", "package", "bug", "code", "programming", "rust"]
```

Adaptar o analisador no `SourcePlanner` para que, se a query contiver termos noticiosos e nenhuma palavra tech explícita, force o domínio para `Domain.NEWS` ou `Domain.UNIVERSAL`.

---

### TAREFA 5.4 — Renderizar Multi-perspectiva via Tom do GDELT no ReportGenerator

**Arquivo:** `src/report_generator.py`

Quando o relatório contiver resultados agregados de clusters onde o campo `tone` (nas `metrics` do GDELT) difira consideravelmente (indicando tons favoráveis vs. críticos), monte e renderize uma seção dedicada:

```python
def _build_perspectives_section(self, results: list) -> str:
    """Mostra espectro de como diferentes fontes cobriram o mesmo evento com base no tom."""
    # Agrupa por cluster_id e lê metrics.tone
    # Filtra clusters com opiniões/tons contrastantes e renderiza uma tabela/lista no relatório MD
```

---

### TAREFA 5.5 — Atualizar Streamlit com Badges de Tom/Sentimento

**Arquivo:** `ui/streamlit_app.py`

Integrar no painel de detalhes dos resultados um pequeno badge visual mostrando a análise de tom/sentimento da notícia:
- Verde / Tom Positivo (> 2.0)
- Cinza / Neutro (-2.0 a 2.0)
- Vermelho / Tom Crítico/Negativo (< -2.0)

---

### TAREFA 5.6 — Testes de Integração e Roteamento

**Arquivo a criar:** `tests/test_routing_universal.py`

Cobrir:
1. Query noticiosa ("O que aconteceu hoje na bolsa") é classificada em `Domain.NEWS` e ativa `gdelt`.
2. Mapeamento do `domains.yaml` é carregado incluindo `universal` e `news`.
3. A renderização de perspectivas não falha quando não há dados de tom.

---

### TAREFA 5.7 — Commit final e encerramento do ciclo

```bash
git add src/types.py config/domains.yaml src/source_planner.py \
        src/report_generator.py ui/streamlit_app.py tests/test_routing_universal.py
git commit -m "feat(parte4/fase5): finaliza roteamento universal, enum de domínios e polimento de tom GDELT"
git push origin main
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 5 (E DO PLANO COMPLETO)

- [ ] `Domain.UNIVERSAL` e `Domain.NEWS` inseridos em `src/types.py`
- [ ] Domínios declarados em `config/domains.yaml` com as fontes da Fase 2
- [ ] `SourcePlanner` roteia para notícias e universal corretamente
- [ ] Seção de perspectivas por tom inserida em `ReportGenerator`
- [ ] Indicadores de tom/sentimento visíveis no Streamlit
- [ ] Testes de integração verdes e sem novas regressões
- [ ] Commit e push concluídos com sucesso no SRA
