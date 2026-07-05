import pytest
from unittest.mock import MagicMock
from src.pipeline.pipeline import PipelineStage, PipelineContext
from src.pipeline.stage_factory import StageFactory, StageFactoryConfig, StageFactoryError

class MockCustomStage(PipelineStage):
    def __init__(self, value: str):
        self.name = "mock_custom"
        self.value = value

    async def run(self, context: PipelineContext) -> PipelineContext:
        if context.metadata is None:
            context.metadata = {}
        context.metadata["custom_val"] = self.value
        return context

def test_stage_factory_di_and_overrides():
    # Setup mocks das dependências
    mock_llm = MagicMock()
    mock_cache = MagicMock()
    mock_orchestrator = MagicMock()

    # 1. Instancia com DI
    factory = StageFactory(
        orchestrator=mock_orchestrator,
        llm_client=mock_llm,
        cache=mock_cache
    )

    # 2. Registra e testa factory customizada
    factory.register("custom_stage", lambda: MockCustomStage("injected_value"))

    stage = factory.create_stage("custom_stage")
    assert isinstance(stage, MockCustomStage)
    assert stage.value == "injected_value"

    # Cache do stage deve funcionar
    stage_cached = factory.create_stage("custom_stage")
    assert stage is stage_cached  # Mesmo objeto devido ao cache

    # 3. Teste de Override (prioridade máxima)
    factory.override("custom_stage", lambda: MockCustomStage("overridden_value"))
    stage_overridden = factory.create_stage("custom_stage")
    assert isinstance(stage_overridden, MockCustomStage)
    assert stage_overridden.value == "overridden_value"
    assert stage_overridden is not stage  # Nova instância ignorou o cache antigo

    # 4. Remove override e volta ao registry normal
    factory.remove_override("custom_stage")
    stage_normal = factory.create_stage("custom_stage")
    assert stage_normal.value == "injected_value"

def test_stage_factory_pipeline_creation():
    mock_llm = MagicMock()
    factory = StageFactory(llm_client=mock_llm)

    # Cria pipeline com stages built-in
    pipeline = factory.create_pipeline(["intent", "expand"])

    assert len(pipeline.stages) == 2
    assert pipeline.stages[0].name == "intent"
    assert pipeline.stages[1].name == "expand"

@pytest.mark.asyncio
async def test_stage_factory_shutdown():
    factory = StageFactory()

    mock_stage = MagicMock(spec=PipelineStage)
    mock_stage.close = MagicMock()

    # Registra e cria stage com close method
    factory.register("stateful", lambda: mock_stage)
    factory.create_stage("stateful")

    # Shutdown deve invocar close do stage
    await factory.shutdown()
    mock_stage.close.assert_called_once()
    assert len(factory.get_cached_stages()) == 0
