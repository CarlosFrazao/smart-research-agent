# MISSÃO PARTE2 — FASE 1: Bloqueantes Críticos (Conectores Enterprise + Wiring Test)

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 1 do plano derivado da `AUDITORIA_SRA_PARTE_2.md`.
> Execute SOMENTE o que está descrito aqui. Não antecipe tarefas de fases posteriores.

---

## 🎯 OBJETIVO DA FASE

Corrigir os dois bloqueantes mais críticos do codebase antes que qualquer fonte nova seja adicionada:
1. Registrar os conectores Enterprise (Notion/Confluence/SharePoint) no `SearcherFactory` — eles são fontes primárias em 5 de 7 domínios, mas **nunca são executados** silenciosamente.
2. Escrever o **teste de integração de wiring** que vai impedir que qualquer módulo futuro caia no mesmo padrão de "implementado mas nunca conectado".

---

## 🛠️ SKILLS A USAR

Carregue estas skills antes de começar (leia o SKILL.md de cada uma):

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `.claude/skills/python-pro/SKILL.md` | Para todo código Python novo |
| `test-driven-development` | `.claude/skills/test-driven-development/SKILL.md` | Para o teste de integração de wiring |
| `clean-code` | `.claude/skills/clean-code/SKILL.md` | Para revisão dos arquivos editados |

---

## 📋 TAREFAS (em ordem obrigatória)

### TAREFA 1.1 — Adicionar credenciais dos conectores ao `Config` e `.env.example`

**Arquivo alvo:** `src/config.py` e `.env.example`

**Contexto:** `NotionClient`, `ConfluenceClient` e `SharePointClient` existem e funcionam, mas `src/config.py` (Pydantic `BaseSettings`) não tem nenhum campo para suas credenciais. Sem isso, nem mesmo é possível configurar a conexão.

**O que fazer:**
1. Abrir `src/config.py` e localizar onde outras credenciais de terceiros são declaradas (ex: ProductHunt, Firecrawl, Spider). Replicar o padrão exato.
2. Adicionar os campos abaixo, todos com `default=None` (opcionais — seguindo o padrão do projeto):
   ```python
   # Enterprise Connectors
   notion_api_key: str | None = None

   confluence_api_key: str | None = None
   confluence_base_url: str | None = None   # ex: https://sua-empresa.atlassian.net

   sharepoint_client_id: str | None = None
   sharepoint_client_secret: str | None = None
   sharepoint_tenant_id: str | None = None
   ```
3. Abrir `.env.example` e adicionar a seção correspondente, com comentários explicativos (seguindo o estilo já existente no arquivo).

**Validação:** `python -c "from src.config import Config; c = Config(); print('ok')"` deve funcionar sem erros.

---

### TAREFA 1.2 — Registrar os três conectores no `SearcherFactory`

**Arquivo alvo:** `src/search/factory.py` → método `create_searchers()`

**Contexto:** O `SearcherFactory.create_searchers()` já registra condicionalmente fontes pagas (ex: Firecrawl, Spider) verificando se a credencial existe. O mesmo padrão deve ser seguido para os três conectores Enterprise.

**O que fazer:**
1. Abrir `src/search/factory.py` e ler como uma fonte condicional já existente é registrada (ex: Firecrawl). Use exatamente o mesmo padrão.
2. Importar `NotionClient`, `ConfluenceClient` e `SharePointClient` de `src/connectors/`.
3. Adicionar o registro condicional de cada um:
   ```python
   # Notion (Enterprise connector)
   if config.notion_api_key:
       searchers["notion"] = NotionClient(api_key=config.notion_api_key)

   # Confluence (Enterprise connector)
   if config.confluence_api_key and config.confluence_base_url:
       searchers["confluence"] = ConfluenceClient(
           api_key=config.confluence_api_key,
           base_url=config.confluence_base_url
       )

   # SharePoint (Enterprise connector)
   if config.sharepoint_client_id and config.sharepoint_client_secret and config.sharepoint_tenant_id:
       searchers["sharepoint"] = SharePointClient(
           client_id=config.sharepoint_client_id,
           client_secret=config.sharepoint_client_secret,
           tenant_id=config.sharepoint_tenant_id
       )
   ```
   > Adapte os parâmetros do construtor ao que os clientes realmente pedem — abra `src/connectors/notion_client.py`, `confluence_client.py` e `sharepoint_client.py` para confirmar a assinatura do `__init__` antes de escrever.

**Validação:** `python -c "from src.search.factory import SearcherFactory; print('import ok')"` deve funcionar sem erros.

---

### TAREFA 1.3 — Adicionar log de warning quando uma fonte do plano não tem searcher

**Arquivo alvo:** `src/pipeline/stages/search_stage.py`

**Contexto:** Quando `self.searchers.get(source_name)` retorna `None`, o código apenas faz `continue` silenciosamente. Isso escondeu o bug dos conectores Enterprise por tempo indeterminado. O fix é adicionar um `logger.warning` nesse ponto.

**O que fazer:**
Localizar o trecho:
```python
searcher = self.searchers.get(source_name)
if not searcher or not getattr(searcher, "enabled", True):
    continue
```
E modificar para:
```python
searcher = self.searchers.get(source_name)
if not searcher:
    logger.warning(
        "Source '%s' is in the search plan but has no registered searcher. "
        "Check SearcherFactory.create_searchers() and Config credentials.",
        source_name,
    )
    continue
if not getattr(searcher, "enabled", True):
    continue
```

---

### TAREFA 1.4 — Escrever o teste de integração de wiring (CRÍTICO — maior alavancagem da auditoria)

**Arquivo alvo:** `tests/test_wiring_integration.py` (arquivo NOVO)

**Contexto:** Este é o teste mais importante de toda a auditoria. Ele garante que qualquer módulo novo que seja adicionado ao futuro seja obrigado a ser conectado ao pipeline real — ou explicitamente marcado como experimental. Sem esse teste, o padrão de "implementado mas nunca conectado" vai se repetir silenciosamente.

**O que fazer:**
Criar `tests/test_wiring_integration.py` com os seguintes testes:

```python
"""
Testes de integração de "fiação" (wiring).

Garantem que todo source_name declarado em DOMAIN_SOURCES/domains.yaml
tem um searcher correspondente registrado no SearcherFactory.

Se este teste falhar após adicionar uma nova fonte, significa que o SearcherFactory
precisa ser atualizado para incluir o novo searcher.
"""
import pytest
from unittest.mock import MagicMock, patch

from src.source_planner import SourcePlanner, DOMAIN_SOURCES
from src.search.factory import SearcherFactory


class TestSourcePlannerToSearcherFactoryWiring:
    """Valida que todo source_name do SourcePlanner existe no SearcherFactory."""

    def _get_all_planned_sources(self) -> set[str]:
        """Coleta todos os source_names únicos de todos os domínios."""
        all_sources: set[str] = set()
        for domain_config in DOMAIN_SOURCES.values():
            all_sources.update(domain_config.get("primary", []))
            all_sources.update(domain_config.get("secondary", []))
        return all_sources

    def _get_registered_searchers(self) -> set[str]:
        """
        Retorna as chaves registradas no SearcherFactory com credenciais mockadas.
        O mock garante que TODOS os searchers condicionais (que exigem API key) sejam
        instanciados, permitindo validar o wiring independente do ambiente.
        """
        mock_config = MagicMock()
        # Ativar todos os conectores opcionais para o teste de wiring
        mock_config.notion_api_key = "test-notion-key"
        mock_config.confluence_api_key = "test-conf-key"
        mock_config.confluence_base_url = "https://test.atlassian.net"
        mock_config.sharepoint_client_id = "test-sp-id"
        mock_config.sharepoint_client_secret = "test-sp-secret"
        mock_config.sharepoint_tenant_id = "test-sp-tenant"
        mock_config.firecrawl_api_key = "test-fc-key"
        mock_config.spider_api_key = "test-spider-key"
        mock_config.producthunt_api_key = "test-ph-key"
        # Adicionar outros campos opcionais conforme necessário

        mock_orchestrator = MagicMock()
        mock_orchestrator.config = mock_config

        with patch("src.search.factory.Config", return_value=mock_config):
            searchers = SearcherFactory.create_searchers(mock_orchestrator)

        return set(searchers.keys())

    def test_all_planned_sources_have_registered_searchers(self):
        """
        CRÍTICO: Todo source_name em DOMAIN_SOURCES deve ter um searcher registrado.
        Se este teste falhar, atualize SearcherFactory.create_searchers().
        """
        planned = self._get_all_planned_sources()
        registered = self._get_registered_searchers()

        missing = planned - registered
        assert not missing, (
            f"Os seguintes sources estão no plano de domínios mas NÃO têm "
            f"searcher registrado no SearcherFactory: {sorted(missing)}\n"
            f"Adicione o registro correspondente em src/search/factory.py"
        )

    def test_source_planner_generates_valid_plan_for_all_domains(self):
        """
        Para cada domínio, o SourcePlanner deve gerar um plano não-vazio
        com sources que existam no SearcherFactory.
        """
        registered = self._get_registered_searchers()
        planner = SourcePlanner()

        for domain in DOMAIN_SOURCES.keys():
            plan = planner.plan(domain=domain, query="test query")
            assert plan is not None, f"SourcePlanner retornou None para domínio '{domain}'"

            all_plan_sources = plan.primary + plan.secondary
            assert all_plan_sources, f"Plano vazio para domínio '{domain}'"

            for source in all_plan_sources:
                assert source in registered, (
                    f"Source '{source}' no plano do domínio '{domain}' "
                    f"não está registrado no SearcherFactory"
                )
```

**Validação:**
```bash
python -m pytest tests/test_wiring_integration.py -v
```
> **Atenção:** Antes de registrar os conectores Enterprise (Tarefas 1.1 e 1.2), este teste **deve falhar** com `notion`, `confluence`, `sharepoint` na lista de missing. Isso confirma que o teste detecta o bug. Execute, confirme a falha, então complete as Tarefas 1.1 e 1.2, e execute novamente para confirmar que passa.

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 1

Todos os itens abaixo devem ser verdadeiros antes de fechar esta fase:

- [ ] `python -m pytest tests/test_wiring_integration.py -v` → **todos os testes passam**
- [ ] `python -m pytest tests/ -k "connector or notion or confluence or sharepoint or factory" -v` → sem regressões
- [ ] `python -m pytest tests/ --tb=short -q` → suíte completa sem novas falhas
- [ ] `python -c "from src.config import Config; c = Config(); print(c.notion_api_key)"` → executa sem erro (retorna None)
- [ ] Nenhum `TODO`, `FIXME` ou `pass` introduzido nos arquivos editados
- [ ] O log de warning do `search_stage.py` está no lugar certo (não dentro do bloco `enabled`, separado)

---

## 🚫 FORA DO ESCOPO DESTA FASE

**NÃO execute nada abaixo** — essas tarefas pertencem a fases posteriores:
- Popular `misinformation_domains.yaml` com dados reais (Fase 2)
- Implementar autenticação na API REST (Fase 3)
- Corrigir `FeedbackRanker` / `ResultID` canônico (Fase 4)
- Implementar `GenericAPISearcher` ou novas fontes (Fase 6+)
