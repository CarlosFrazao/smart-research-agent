import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.gap_detector import GapDetector, GapDetectionState
from src.types import RankedResult, IntentResult, Domain, Intention

@pytest.fixture
def intent():
    return IntentResult(
        domain=Domain.SAAS_B2B,
        entities=["CRM"],
        intention=Intention.DISCOVER,
        urgency="nao",
        confidence="alta",
    )

def make_results(n, sources=None):
    sources = sources or ["github", "reddit", "arxiv"]
    return [
        RankedResult(
            source=sources[i % len(sources)],
            title=f"project{i}",
            url=f"https://github.com/{i}",
            description="d",
            score=50.0,
        )
        for i in range(n)
    ]

@pytest.mark.asyncio
async def test_gap_detector_injects_prism_low_confidence(intent):
    llm = MagicMock()
    # Mock para retorno do LLM estruturado
    llm.generate_structured = AsyncMock(return_value={
        "gaps": [],
        "is_complete": True,
        "rationale": "Test validation",
        "new_queries": []
    })

    # Patch no loader de personas
    with patch("src.gap_detector.AgentPersonaLoader") as MockLoaderClass:
        mock_loader = MockLoaderClass.return_value
        mock_loader.load.return_value = "# Prism Scientist Persona Content"
        mock_loader.build_enhanced_prompt.side_effect = lambda base, name: f"# Prism Scientist Persona Content\n\n---\n\n{base}"

        detector = GapDetector(llm)
        # Força o fallback para o modo cirurgia/black_ops ou simula um estado que injete
        detector._last_operation_mode = "cirurgia"
        
        state = GapDetectionState(accumulated_cost_usd=0.0)

        # Roda a detecção de gaps (precisa de pelo menos 10 resultados para passar de algumas heurísticas de corte)
        await detector.detect(make_results(15), "crm", intent, state=state)

    # Verificações
    mock_loader.build_enhanced_prompt.assert_called_once()
    assert mock_loader.build_enhanced_prompt.call_args[0][1] == "prism_scientist"
