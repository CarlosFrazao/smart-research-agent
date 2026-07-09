import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.pipeline.stages.verification_stage import VerificationStage
from src.pipeline.pipeline import PipelineContext
from src.types import RankedResult

@pytest.mark.asyncio
async def test_verification_stage_scout_integration():
    # Mocks
    mock_llm = AsyncMock()
    mock_llm.generate = AsyncMock(return_value="# Scout Architecture Map Content")
    
    mock_code_agent = MagicMock()
    
    # Mock do loader
    with patch("src.pipeline.stages.verification_stage.AgentPersonaLoader") as MockLoaderClass:
        mock_loader = MockLoaderClass.return_value
        mock_loader.load.return_value = "# Scout Persona"

        stage = VerificationStage(code_agent=mock_code_agent, llm_client=mock_llm)
        
        # Cria context com um resultado do GitHub e um resultado genérico
        context = PipelineContext(query="test scout")
        results = [
            RankedResult(
                source="github",
                entity="entity_github",
                title="Concorrente Git",
                description="Repositório Python de webhook para WhatsApp e Shopee",
                url="https://github.com/carlos/shopee-bot",
                sources=["github"],
                combined_score=0.9,
                score=90.0,
            ),
            RankedResult(
                source="web",
                entity="entity_web",
                title="Generic Web Page",
                description="Some description",
                url="https://example.com/page",
                sources=["web"],
                combined_score=0.5,
                score=50.0,
            )
        ]
        context.ranked_results = results

        # Executa
        await stage.run(context)

    # Verificações
    assert "repo_architectures" in context.extra
    repo_archs = context.extra["repo_architectures"]
    assert len(repo_archs) == 1
    assert repo_archs[0]["url"] == "https://github.com/carlos/shopee-bot"
    assert repo_archs[0]["architecture_map"] == "# Scout Architecture Map Content"
    
    assert mock_llm.generate.call_count == 3
    # Verifica se a chamada do Scout contém a persona e a url do repo
    scout_call = mock_llm.generate.call_args_list[2]
    prompt_sent = scout_call[0][0]
    assert "# Scout Persona" in prompt_sent
    assert "https://github.com/carlos/shopee-bot" in prompt_sent
