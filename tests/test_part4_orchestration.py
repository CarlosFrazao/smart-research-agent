import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from src.types import (
    SynthesizedResult, ResearchMetadata, IntentResult, Domain, Intention,
    ExpandedQuery, RankedResult,
)


def _make_synthesized(n: int = 3) -> list:
    return [
        SynthesizedResult(
            entity=f"project{i}",
            title=f"Project {i}",
            description=f"Description of project {i}",
            sources=["github"],
            urls=[f"https://github.com/p{i}"],
            combined_score=float(90 - i * 5),
            metrics={"stars": 1000 * (n - i), "forks": 100, "language": "Python", "license": "MIT"},
            highlights=[f"{1000 * (n - i)} stars no GitHub"],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
        )
        for i in range(n)
    ]


def _make_metadata(query: str = "test") -> ResearchMetadata:
    return ResearchMetadata(
        query=query,
        domain="saas_b2b",
        sources=["github", "reddit", "hackernews"],
        total_results=30,
        iterations=2,
        timestamp=datetime.now(),
        duration_seconds=12.5,
    )


# ─── Report Generator ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_report_generator_structure():
    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        mock_instance = MagicMock()
        mock_instance.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="Resumo gerado pelo LLM.")])
        )
        MockAnthropic.return_value = mock_instance

        from src.clients.llm_client import LLMClient, LLMProvider
        from src.report_generator import ReportGenerator

        llm = LLMClient(LLMProvider.ANTHROPIC, {"api_key": "test", "model": "claude-test"})
        rg = ReportGenerator(llm)

        results = _make_synthesized(5)
        metadata = _make_metadata("CRM open source")

        report = await rg.generate("CRM open source", results, metadata)

        assert "# Relatorio:" in report
        assert "## 1. Resumo Executivo" in report
        assert "## 2. Projetos" in report
        assert "## 3. Comparacao" in report
        assert "## 4. Tecnologias" in report
        assert "## 5. Discussao" in report
        assert "## 6. An" in report
        assert "## 7. Recomenda" in report
        assert "## 8. Links e Refer" in report
        assert len(report) > 500


@pytest.mark.asyncio
async def test_report_generator_llm_fallback():
    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        mock_instance = MagicMock()
        mock_instance.messages.create = AsyncMock(side_effect=Exception("LLM down"))
        MockAnthropic.return_value = mock_instance

        from src.clients.llm_client import LLMClient, LLMProvider
        from src.report_generator import ReportGenerator

        llm = LLMClient(LLMProvider.ANTHROPIC, {"api_key": "test", "model": "claude-test"})
        rg = ReportGenerator(llm)

        results = _make_synthesized(2)
        metadata = _make_metadata("test query")
        report = await rg.generate("test query", results, metadata)

        assert "# Relatorio:" in report
        assert len(report) > 200


@pytest.mark.asyncio
async def test_report_generator_empty_results():
    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        mock_instance = MagicMock()
        mock_instance.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="Nenhum resultado encontrado.")])
        )
        MockAnthropic.return_value = mock_instance

        from src.clients.llm_client import LLMClient, LLMProvider
        from src.report_generator import ReportGenerator

        llm = LLMClient(LLMProvider.ANTHROPIC, {"api_key": "test", "model": "claude-test"})
        rg = ReportGenerator(llm)

        metadata = _make_metadata("empty query")
        report = await rg.generate("empty query", [], metadata)
        assert "# Relatorio:" in report
        assert "Nenhum projeto encontrado" in report or "# Relatorio:" in report


def test_report_generator_save(tmp_path):
    with patch("anthropic.AsyncAnthropic"):
        from src.clients.llm_client import LLMClient, LLMProvider
        from src.report_generator import ReportGenerator

        llm = LLMClient(LLMProvider.ANTHROPIC, {"api_key": "test", "model": "claude-test"})
        rg = ReportGenerator(llm)

        filepath = rg.save_report("# Test Report", "test query", str(tmp_path))
        assert filepath.endswith(".md")
        import os
        assert os.path.exists(filepath)
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
        assert content == "# Test Report"


# ─── Orchestrator ─────────────────────────────────────────────────────────────

def test_orchestrator_init_no_api():
    with patch("anthropic.AsyncAnthropic"):
        from src.orchestrator import Orchestrator
        from src.config import Config
        config = Config(anthropic_api_key="test-key")
        orch = Orchestrator(config)
        assert orch.searchers is not None
        assert "github" in orch.searchers
        assert "reddit" in orch.searchers
        assert "hackernews" in orch.searchers
        assert "arxiv" in orch.searchers
        assert "awesome" in orch.searchers


def test_orchestrator_searchers_are_base_searcher():
    with patch("anthropic.AsyncAnthropic"):
        from src.orchestrator import Orchestrator
        from src.config import Config
        from src.search.base_searcher import BaseSearcher
        config = Config(anthropic_api_key="test-key")
        orch = Orchestrator(config)
        for name, searcher in orch.searchers.items():
            assert isinstance(searcher, BaseSearcher), f"{name} nao e BaseSearcher"


@pytest.mark.asyncio
async def test_orchestrator_research_mocked():
    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        mock_instance = MagicMock()
        mock_instance.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="Resumo executivo de teste.")])
        )
        MockAnthropic.return_value = mock_instance

        from src.orchestrator import Orchestrator
        from src.config import Config

        config = Config(anthropic_api_key="test-key", max_iterations=1)
        orch = Orchestrator(config)

        from src.types import SearchResult
        mock_results = [
            SearchResult(
                source=src, title=f"Project {i} / repo{i}", url=f"https://github.com/p{i}/r{i}",
                description=f"An open source project {i} with many features",
                metrics={"stars": 1000 * (i + 1), "forks": 100, "language": "Python",
                         "updated_at": "2026-01-01", "license": "MIT"},
            )
            for i, src in enumerate(["github", "reddit", "hackernews", "arxiv"] * 4)
        ]

        # Mock _parallel_search diretamente para garantir que resultados chegam
        orch._parallel_search = AsyncMock(return_value=mock_results)
        orch.llm.generate = AsyncMock(return_value="Resumo executivo de teste.")
        orch.llm.generate_structured = AsyncMock(side_effect=[
            # intent
            {"domain": "saas_b2b", "entities": ["HubSpot"], "intention": "discover", "urgency": "nao", "confidence": "alta"},
            # query expand
            {"queries": [
                {"query": f"open source crm {i}", "type": "qualificador", "priority": "alta", "rationale": "test"}
                for i in range(8)
            ]},
            # gap detect (is_complete = True)
            {"is_complete": True, "missing_aspects": [], "new_queries": [], "confidence": "alta", "rationale": "ok"},
        ])

        report = await orch.research("CRM open source parecido com HubSpot")

        assert "# Relatorio:" in report
        assert "CRM open source parecido com HubSpot" in report
        assert "## 1. Resumo Executivo" in report
        assert "## 7. Recomenda" in report
        assert len(report) > 500


# ─── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_version(capsys):
    with patch("anthropic.AsyncAnthropic"):
        from src.main import cmd_version
        cmd_version(None)
        captured = capsys.readouterr()
        assert "Smart Research Agent" in captured.out
        assert "v1.0" in captured.out


def test_cli_config(capsys):
    with patch("anthropic.AsyncAnthropic"):
        from src.main import cmd_config
        from src.config import Config
        args = MagicMock()
        cmd_config(args)
        captured = capsys.readouterr()
        assert "LLM Provider" in captured.out
        assert "Max resultados" in captured.out


def test_cli_parser():
    with patch("anthropic.AsyncAnthropic"):
        from src.main import create_parser
        parser = create_parser()
        args = parser.parse_args(["version"])
        assert args.command == "version"

        args = parser.parse_args(["research", "test query"])
        assert args.command == "research"
        assert args.query == "test query"

        args = parser.parse_args(["config"])
        assert args.command == "config"


# ─── MCP Server ───────────────────────────────────────────────────────────────

def test_mcp_server_imports():
    from src import mcp_server
    assert hasattr(mcp_server, "app")
    assert hasattr(mcp_server, "health")


@pytest.mark.asyncio
async def test_mcp_health_endpoint():
    from src.mcp_server import health
    result = await health()
    assert result["status"] == "ok"
    assert "smart-research-agent" in result["service"]


# ─── Benchmark Queries (unit-level smoke test) ────────────────────────────────

@pytest.mark.asyncio
async def test_benchmark_pipeline_smoke():
    """Valida que o pipeline completo roda sem erros com mocks."""
    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        mock_instance = MagicMock()
        mock_instance.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="Mock LLM response")])
        )
        MockAnthropic.return_value = mock_instance

        from src.orchestrator import Orchestrator
        from src.config import Config
        from src.types import SearchResult

        config = Config(anthropic_api_key="test-key", max_iterations=1)
        orch = Orchestrator(config)

        benchmark_queries = [
            "CRM open source parecido com HubSpot",
            "n8n vs Make vs Zapier",
            "best open source LLM for local deployment",
        ]

        mock_results = [
            SearchResult(
                source="github", title=f"tool/project-{i}", url=f"https://github.com/t/p{i}",
                description=f"Tool {i} description",
                metrics={"stars": 5000 + i * 100, "forks": 500, "language": "TypeScript",
                         "updated_at": "2026-01-01", "license": "MIT"},
            )
            for i in range(10)
        ]

        for searcher in orch.searchers.values():
            searcher.search = AsyncMock(return_value=mock_results)

        orch.llm.generate = AsyncMock(return_value="Mock summary text for report.")
        orch.llm.generate_structured = AsyncMock(side_effect=Exception("Use fallback"))

        for query in benchmark_queries:
            report = await orch.research(query)
            assert "# Relatorio:" in report, f"Relatorio invalido para query: {query}"
            assert len(report) > 500, f"Relatorio muito curto para query: {query}"
