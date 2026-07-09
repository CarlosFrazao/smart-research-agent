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
import pytest
from unittest.mock import MagicMock

from src.source_planner import SourcePlanner, DOMAIN_SOURCES
from src.search.factory import SearcherFactory
from src.types import Domain, Intention, IntentResult, ExpandedQuery


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
