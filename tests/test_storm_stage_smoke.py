"""Smoke test manual (sem pytest) da StormStage.

Valida a integração real com `ResearchPipeline` (não apenas o contrato
mockado): posição entre IntentStage/ExpandStage, população de
`context.extra["storm_perspectives"]`/`storm_seed_queries"]`, no-op quando
`enabled=False` e resiliência (`critical=False`) quando o
`StormPerspectiveGenerator` falha de forma inesperada.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.pipeline import ResearchPipeline
from src.pipeline.stages import StormStage
from src.storm_perspectives import StormPerspectiveGenerator


async def test_storm_stage_happy_path():
    llm = MagicMock()
    mock_perspectives = [
        {
            "name": "Cloud Engineer",
            "description": "Foca em custo e latência.",
            "sub_queries": ["SQLite replication latency", "SQLite AWS EBS performance"],
        },
        {
            "name": "Database Administrator",
            "description": "Foca em locks e backups.",
            "sub_queries": ["SQLite write lock concurrency", "SQLite replication latency"],
        },
    ]
    llm.generate_structured = AsyncMock(return_value=mock_perspectives)

    generator = StormPerspectiveGenerator(llm_client=llm)
    stage = StormStage(storm_generator=generator, num_perspectives=2)

    pipeline = ResearchPipeline([stage], name="storm_only")
    context = await pipeline.run("SQLite in Enterprise SaaS")

    assert context.completed_stages == ["storm"]
    assert context.get("storm_perspectives") == mock_perspectives
    # Deduplicada: "SQLite replication latency" aparece nas duas personas
    assert context.get("storm_seed_queries") == [
        "SQLite replication latency",
        "SQLite AWS EBS performance",
        "SQLite write lock concurrency",
    ]
    # Contrato do ExpandStage não é tocado (stage é aditivo, não substitui)
    assert context.expanded_queries == []
    print("✅ test_storm_stage_happy_path passou")


async def test_storm_stage_disabled_is_noop():
    stage = StormStage(llm_client=MagicMock(), enabled=False)

    pipeline = ResearchPipeline([stage], name="storm_disabled")
    context = await pipeline.run("qualquer tópico")

    assert context.completed_stages == ["storm"]
    assert context.get("storm_perspectives") is None
    assert context.get("storm_seed_queries") is None
    print("✅ test_storm_stage_disabled_is_noop passou")


async def test_storm_stage_failure_is_non_critical():
    generator = MagicMock()
    generator.generate_perspectives_with_queries = AsyncMock(
        side_effect=RuntimeError("bug inesperado")
    )
    stage = StormStage(storm_generator=generator)
    assert stage.critical is False

    pipeline = ResearchPipeline([stage], name="storm_failure")
    # Não deve levantar PipelineError: stage não-crítica só registra o erro.
    context = await pipeline.run("tópico qualquer")

    assert context.completed_stages == ["storm"]
    assert len(context.errors) == 1
    assert context.errors[0].stage == "storm"
    assert context.errors[0].critical is False
    assert context.get("storm_perspectives") is None
    print("✅ test_storm_stage_failure_is_non_critical passou")


async def main() -> None:
    await test_storm_stage_happy_path()
    await test_storm_stage_disabled_is_noop()
    await test_storm_stage_failure_is_non_critical()


if __name__ == "__main__":
    asyncio.run(main())
