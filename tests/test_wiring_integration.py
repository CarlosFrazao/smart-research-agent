"""
Testes de integração de "fiação" (wiring).

Garantem que todo source_name declarado em DOMAIN_SOURCES (em src/source_planner.py,
espelhado por config/domains.yaml) tem um searcher correspondente registrado no
SearcherFactory.

Se este teste falhar após adicionar uma nova fonte, significa que o SearcherFactory
precisa ser atualizado para incluir o novo searcher.

Notas de adaptação à assinatura real do codebase:
- ``SourcePlanner.plan()`` recebe ``(intent: IntentResult, queries: list[ExpandedQuery])``,
  NÃO ``domain=``/``query=``. Os testes constroem um ``IntentResult`` real (com ``Domain``)
  e uma lista de ``ExpandedQuery`` real.
- ``SearcherFactory.create_searchers()`` lê ``orchestrator.config`` diretamente (nunca
  instancia ``Config()`` internamente), então o teste injeta um ``MagicMock`` como
  ``orchestrator.config`` com todas as credenciais opcionais ativadas.
"""
import re
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from src.source_planner import SourcePlanner, DOMAIN_SOURCES
from src.search.factory import SearcherFactory
from src.types import Domain, Intention, IntentResult, ExpandedQuery

# Raiz do projeto (pai de tests/), usada para localizar prompts/ e src/.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


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

        O mock garante que TODOS os searchers condicionais (que exigem API key)
        sejam instanciados, permitindo validar o wiring independente do ambiente.
        """
        mock_config = MagicMock()
        # Ativar todos os conectores opcionais para o teste de wiring
        mock_config.notion_api_key = "test-notion-key"
        mock_config.confluence_api_key = "test-conf-key"
        mock_config.confluence_base_url = "https://test.atlassian.net"
        mock_config.confluence_username = "test@company.com"
        mock_config.sharepoint_client_id = "test-sp-id"
        mock_config.sharepoint_client_secret = "test-sp-secret"
        mock_config.sharepoint_tenant_id = "test-sp-tenant"
        mock_config.firecrawl_api_key = "test-fc-key"
        mock_config.spider_api_key = "test-spider-key"
        mock_config.producthunt_api_key = "test-ph-key"
        # Campos adicionais usados pelo factory para habilitar searchers condicionais
        mock_config.spider_enabled = False
        mock_config.steel_enabled = False
        mock_config.host_mode = False
        mock_config.playwright_enabled = False
        # Adicionar outros campos opcionais conforme necessário

        mock_orchestrator = MagicMock()
        mock_orchestrator.config = mock_config

        # NOTA: create_searchers() consome orchestrator.config diretamente; não
        # instancia Config() internamente, portanto nenhum patch de Config é necessário.
        searchers = SearcherFactory.create_searchers(mock_orchestrator)

        return set(searchers.keys())

    def _make_intent(self, domain_key: str) -> IntentResult:
        """Constrói um IntentResult válido para o domínio informado."""
        return IntentResult(
            domain=Domain(domain_key),
            intention=Intention.DISCOVER,
            urgency="nao",
            confidence="media",
        )

    def _make_queries(self) -> list[ExpandedQuery]:
        """Constrói uma lista mínima de ExpandedQuery compatível com plan()."""
        return [
            ExpandedQuery(query="test query", type="synonym", priority="alta"),
            ExpandedQuery(query="test query perspective", type="perspective", priority="media"),
        ]

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
            intent = self._make_intent(domain)
            plan = planner.plan(intent, self._make_queries())
            assert plan is not None, f"SourcePlanner retornou None para domínio '{domain}'"

            all_plan_sources = plan.primary + plan.secondary
            assert all_plan_sources, f"Plano vazio para domínio '{domain}'"

            for source in all_plan_sources:
                assert source in registered, (
                    f"Source '{source}' no plano do domínio '{domain}' "
                    f"não está registrado no SearcherFactory"
                )


class TestPromptsWiring:
    """Valida que arquivos em prompts/ têm referência explícita no código ou são
    marcados como não-ativos (documentação de referência).

    Se um novo ``prompts/*.md`` for adicionado sem ser carregado pelo código E sem
    estar em ``KNOWN_UNLOADED_PROMPTS``, este teste falha — forçando uma decisão
    consciente (Opção A: conectar ao código; Opção B: marcar como referência;
    Opção C: remover).
    """

    # Prompts intencionalmente não-carregados, marcados como documentação de
    # referência com o aviso HTML no topo (Fase 5, Tarefa 5.2 — Opção B).
    # O prompt real está inline no src/<modulo>.py correspondente.
    KNOWN_UNLOADED_PROMPTS = {
        "gap_detector.md",
        "intent_analyzer.md",
        "query_expander.md",
        "ranker_system.md",
        "report_generator.md",
        "synthesizer.md",
    }

    def _find_loaded_prompts(self) -> set[str]:
        """Busca referências a arquivos ``prompts/*.md`` no código de produção.

        Um prompt é considerado "carregado" se seu nome de arquivo (ex.
        ``source_planner.md``) aparece literalmente em qualquer módulo Python
        sob ``src/``. Isso cobre tanto ``open("prompts/source_planner.md")``
        quanto o padrão do ``AgentPersonaLoader`` (``f"{agent_name}.md"`` com
        checagem de existência no diretório).

        Returns:
            Conjunto de nomes de arquivo (``*.md``) referenciados no código.
        """
        src_dir = _PROJECT_ROOT / "src"
        if not src_dir.exists():
            return set()

        # Nomes de prompt candidatos presentes no diretório prompts/ (topo).
        prompts_dir = _PROJECT_ROOT / "prompts"
        candidate_names = {f.name for f in prompts_dir.glob("*.md")} if prompts_dir.exists() else set()
        if not candidate_names:
            return set()

        loaded: set[str] = set()
        md_literal = re.compile(r"[\"']([\w\-./\\]+\.md)[\"']")

        for py_file in src_dir.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for match in md_literal.findall(content):
                basename = Path(match.replace("\\", "/")).name
                if basename in candidate_names:
                    loaded.add(basename)

        return loaded

    def test_unloaded_prompts_are_explicitly_documented(self):
        """Todo prompt não-carregado deve estar em ``KNOWN_UNLOADED_PROMPTS``.

        Isso garante que cada ``.md`` em ``prompts/`` teve seu destino decidido
        explicitamente: ou é carregado pelo código, ou foi conscientemente
        marcado como documentação de referência.
        """
        prompts_dir = _PROJECT_ROOT / "prompts"
        if not prompts_dir.exists():
            pytest.skip("prompts/ directory not found")

        all_prompt_files = {f.name for f in prompts_dir.glob("*.md")}
        loaded_prompts = self._find_loaded_prompts()

        unloaded = all_prompt_files - loaded_prompts
        undocumented = unloaded - self.KNOWN_UNLOADED_PROMPTS

        assert not undocumented, (
            f"Prompts não-carregados sem decisão documentada: {sorted(undocumented)}\n"
            f"Adicione ao KNOWN_UNLOADED_PROMPTS (se intencional, com o aviso de "
            f"referência no topo do .md) ou conecte ao código correspondente."
        )

    def test_known_unloaded_prompts_still_exist(self):
        """Cada entrada de ``KNOWN_UNLOADED_PROMPTS`` deve existir em prompts/.

        Se um prompt marcado como referência for removido, esta lista deve ser
        atualizada — evitando exceções obsoletas acumuladas.
        """
        prompts_dir = _PROJECT_ROOT / "prompts"
        if not prompts_dir.exists():
            pytest.skip("prompts/ directory not found")

        existing = {f.name for f in prompts_dir.glob("*.md")}
        stale = self.KNOWN_UNLOADED_PROMPTS - existing

        assert not stale, (
            f"Entradas obsoletas em KNOWN_UNLOADED_PROMPTS (arquivo não existe mais): "
            f"{sorted(stale)}"
        )


class TestExperimentalModulesWiring:
    """Valida que módulos não conectados ao pipeline principal são declarados
    explicitamente como experimentais (WIP) em ``EXPERIMENTAL_MODULES.md``.

    Fase 5, Tarefa 5.4: ``react_orchestrator.py`` e ``decision_engine.py`` compõem
    uma arquitetura de orquestração ReAct alternativa, testada isoladamente mas
    nunca conectada ao ``Orchestrator`` de produção. A decisão foi mantê-los como
    WIP explícito — este teste garante que essa decisão permaneça documentada.
    """

    # Módulos que existem no repo mas não são instanciados por nenhum ponto de
    # entrada de produção (api/cli/mcp). Devem estar em EXPERIMENTAL_MODULES.md.
    KNOWN_EXPERIMENTAL_MODULES = {
        "src/react_orchestrator.py",
        "src/decision_engine.py",
    }

    def test_experimental_modules_exist(self):
        """Cada módulo experimental declarado deve existir no repositório."""
        missing = {
            rel for rel in self.KNOWN_EXPERIMENTAL_MODULES
            if not (_PROJECT_ROOT / rel).exists()
        }
        assert not missing, (
            f"Módulos experimentais declarados mas inexistentes: {sorted(missing)}\n"
            f"Remova-os de KNOWN_EXPERIMENTAL_MODULES e de EXPERIMENTAL_MODULES.md."
        )

    def test_experimental_modules_are_documented(self):
        """Cada módulo experimental deve estar citado em EXPERIMENTAL_MODULES.md."""
        doc_path = _PROJECT_ROOT / "EXPERIMENTAL_MODULES.md"
        assert doc_path.exists(), (
            "EXPERIMENTAL_MODULES.md não encontrado na raiz do projeto. "
            "Crie-o para documentar módulos WIP não conectados ao pipeline."
        )

        doc_content = doc_path.read_text(encoding="utf-8")
        undocumented = {
            rel for rel in self.KNOWN_EXPERIMENTAL_MODULES
            if rel not in doc_content
        }
        assert not undocumented, (
            f"Módulos experimentais sem entrada em EXPERIMENTAL_MODULES.md: "
            f"{sorted(undocumented)}"
        )

    def test_experimental_modules_not_used_by_production_entrypoints(self):
        """Módulos experimentais NÃO devem ser instanciados por api/cli/mcp.

        Se um destes módulos passar a ser referenciado por um ponto de entrada de
        produção, ele deixou de ser experimental: promova-o (remova daqui e de
        EXPERIMENTAL_MODULES.md) conforme os critérios documentados.
        """
        entrypoints = [
            _PROJECT_ROOT / "api" / "main.py",
            _PROJECT_ROOT / "cli" / "main.py",
            _PROJECT_ROOT / "src" / "mcp_server.py",
        ]
        # Símbolos que indicariam uso em produção.
        forbidden_symbols = ("react_orchestrator", "ReActOrchestrator")

        offenders: list[str] = []
        for entry in entrypoints:
            if not entry.exists():
                continue
            content = entry.read_text(encoding="utf-8")
            for symbol in forbidden_symbols:
                if symbol in content:
                    offenders.append(f"{entry.name}: usa '{symbol}'")

        assert not offenders, (
            "Módulo experimental referenciado por ponto de entrada de produção:\n"
            + "\n".join(offenders)
            + "\nPromova o módulo (atualize EXPERIMENTAL_MODULES.md e este teste)."
        )
