import pytest
from datetime import datetime
from src.types import SynthesizedResult, ResearchMetadata
from src.report_generator import ReportGenerator

@pytest.mark.asyncio
async def test_report_generator_v2_confidence_annotations():
    # Instancia o generator com mock do LLM
    mock_llm = pytest.importorskip("unittest.mock").MagicMock()
    mock_llm.generate = pytest.importorskip("unittest.mock").AsyncMock(return_value="Resumo de teste")
    
    generator = ReportGenerator(mock_llm)
    
    # 1. Cria resultados sintetizados com as métricas e flags de confiança da Fase 2
    r1 = SynthesizedResult(
        entity="Project A",
        title="Project A",
        description="A great verified tool.",
        sources=["github"],
        urls=["https://github.com/project-a"],
        combined_score=85.0,
        metrics={
            "stars": 120, 
            "dead_links": ["https://badsite.com/broken-url"]
        },
        highlights=["Fast", "Secure"],
        first_seen=datetime.now(),
        last_seen=datetime.now(),
        verdict="Foca",
        tldr="TLDR do projeto A",
        next_step="Ação do projeto A",
        read_min=3
    )
    
    # Injeta atributos de confiança dinâmicos
    r1.evidence_quality = "verified"
    r1.hallucination_flags = ["stale_content", "dead_links_detected"]
    
    metadata = ResearchMetadata(
        query="test query",
        domain="dev_tools",
        sources=["github"],
        total_results=1,
        iterations=1,
        timestamp=datetime.now(),
        duration_seconds=5.0,
        overall_confidence=0.85,
        low_confidence_warnings=[]
    )
    
    report = await generator.generate("test query", [r1], metadata)
    
    # 2. Assegura que o badge de qualidade e alertas foram impressos na Seção 2
    assert "🌟 Verificado (Alta Confiança)" in report
    assert "⚠️ **Fonte Única (Single Source)**" in report
    assert "🚫 **Alertas:** Conteúdo Desatualizado, Links Quebrados Detectados" in report
    
    # 3. Assegura que a seção de links quebrados/inválidos foi listada
    assert "### Links Inválidos ou Quebrados Detectados" in report
    assert "❌ https://badsite.com/broken-url" in report
