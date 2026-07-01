import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime
from src.debate_orchestrator import DebateOrchestrator, Hypothesis, DebateRound
from src.clients.llm_client import LLMClient
from src.types import SearchResult, ExpandedQuery


@pytest.mark.asyncio
async def test_debate_orchestrator_parse_hypotheses():
    llm_mock = MagicMock(spec=LLMClient)
    # Mock do retorno do LLM com JSON de hipóteses
    llm_mock.generate = AsyncMock(return_value="""
[
  {
    "id": "H1",
    "claim": "Prisma ORM e melhor para iniciantes devido a facilidade de uso.",
    "rationale": "Sua sintaxe declarativa reduz curva de aprendizado.",
    "stance": "pro"
  },
  {
    "id": "H2",
    "claim": "SQLAlchemy e superior para queries complexas e performance.",
    "rationale": "Controle granular e otimizacoes de baixo nivel.",
    "stance": "contra"
  }
]
""")
    searchers = {}
    orchestrator = DebateOrchestrator(llm_client=llm_mock, searchers=searchers, num_hypotheses=2)
    hypotheses = await orchestrator.generate_hypotheses("Prisma vs SQLAlchemy")

    assert len(hypotheses) == 2
    assert hypotheses[0].id == "H1"
    assert hypotheses[0].stance == "pro"
    assert hypotheses[1].id == "H2"
    assert hypotheses[1].stance == "contra"


@pytest.mark.asyncio
async def test_debate_orchestrator_judge_round():
    llm_mock = MagicMock(spec=LLMClient)
    # Mock do retorno do LLM juiz com veredito JSON
    llm_mock.generate = AsyncMock(return_value="""
{
  "winner": "H2",
  "confidence": 0.85,
  "reasoning": "SQLAlchemy oferece maior flexibilidade e maturidade.",
  "verdict": "SQLAlchemy e a melhor escolha para sistemas de alta performance."
}
""")
    searchers = {}
    orchestrator = DebateOrchestrator(llm_client=llm_mock, searchers=searchers)

    hypotheses = [
        Hypothesis(id="H1", claim="Prisma e melhor", rationale="Facil", stance="pro"),
        Hypothesis(id="H2", claim="SQLAlchemy e melhor", rationale="Robusto", stance="contra")
    ]

    debate_round = await orchestrator.judge_round("Prisma vs SQLAlchemy", hypotheses)
    assert debate_round.winner == "H2"
    assert debate_round.confidence == 0.85
    assert "alta performance" in debate_round.verdict


@pytest.mark.asyncio
async def test_debate_orchestrator_run_debate_with_search():
    llm_mock = MagicMock(spec=LLMClient)
    # Mock do searcher
    web_searcher_mock = MagicMock()
    web_searcher_mock.search = AsyncMock(return_value=[
        SearchResult(
            source="web",
            title="Prisma ORM benchmark",
            url="https://prisma.io/bench",
            description="Prisma is fast and easy to setup in Node.js applications.",
            metrics={},
            raw={}
        )
    ])
    searchers = {"web": web_searcher_mock}

    orchestrator = DebateOrchestrator(llm_client=llm_mock, searchers=searchers)
    hypotheses = [
        Hypothesis(id="H1", claim="Prisma e rapido", rationale="Bench", stance="pro")
    ]

    updated_hypotheses = await orchestrator.run_debate("Prisma benchmark", hypotheses)
    assert len(updated_hypotheses) == 1
    assert len(updated_hypotheses[0].evidence) == 1
    assert "Prisma is fast" in updated_hypotheses[0].evidence[0]
    assert updated_hypotheses[0].confidence > 0.0


def test_debate_orchestrator_format_markdown():
    llm_mock = MagicMock(spec=LLMClient)
    orchestrator = DebateOrchestrator(llm_client=llm_mock, searchers={})

    hypotheses = [
        Hypothesis(id="H1", claim="H1 claim", rationale="H1 rationale", stance="pro", evidence=["[web] Prisma is good"], sources=["https://prisma.io"], confidence=0.8, search_results_count=1)
    ]
    debate_round = DebateRound(
        query="test query",
        hypotheses=hypotheses,
        winner="H1",
        verdict="H1 wins",
        confidence=0.9,
        reasoning="H1 has better evidence"
    )

    md = orchestrator.format_debate_markdown(debate_round)
    assert "# 🗣️ Relatório de Debate Multi-Agente" in md
    assert "test query" in md
    assert "H1 wins" in md
    assert "H1 has better evidence" in md
    assert "Prisma is good" in md


@pytest.mark.asyncio
async def test_orchestrator_routes_to_debate():
    from unittest.mock import patch
    with patch("anthropic.AsyncAnthropic"), patch("src.clients.llm_client.LLMClient") as MockLLM:
        from src.orchestrator import Orchestrator
        from src.config import Config
        from src.operation_modes import OperationModes

        cfg = Config()
        cfg.memory_enabled = False

        orch = Orchestrator(cfg)
        orch.llm = MockLLM()
        orch.operation_mode = OperationModes.get_mode("debate")

        mock_debate = MagicMock()
        mock_debate.run = AsyncMock(return_value=DebateRound(
            query="Prisma vs SQLAlchemy",
            hypotheses=[],
            winner="H1",
            verdict="Prisma is easier.",
            confidence=0.8,
            reasoning="..."
        ))
        mock_debate.format_debate_markdown = MagicMock(return_value="# Debate Report\nWinner: H1")

        with patch("src.debate_orchestrator.DebateOrchestrator", return_value=mock_debate):
            # Evita erros de salvar relatórios reais no ambiente de teste
            orch.report_generator.save_report = MagicMock(return_value="reports/test.md")
            
            report = await orch.research("Prisma vs SQLAlchemy")

            assert "Winner: H1" in report
            mock_debate.run.assert_called_once_with("Prisma vs SQLAlchemy")
            mock_debate.format_debate_markdown.assert_called_once()
