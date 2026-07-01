import pytest
from src.sentiment_analyzer import SentimentAnalyzer
from src.types import SynthesizedResult


@pytest.fixture
def analyzer():
    return SentimentAnalyzer()


def test_fallback_score_positive(analyzer):
    """Verifica se o analisador léxico de fallback identifica tom positivo."""
    text = "Este projeto é excelente, rápido e inovador. Eu recomendo."
    scores = analyzer._fallback_score(text)
    assert scores["pos"] > 0
    assert scores["neg"] == 0
    assert scores["compound"] > 0


def test_fallback_score_negative(analyzer):
    """Verifica se o analisador léxico de fallback identifica tom negativo."""
    text = "Ferramenta muito lenta, cheia de bugs e problemas. Ruim."
    scores = analyzer._fallback_score(text)
    assert scores["neg"] > 0
    assert scores["pos"] == 0
    assert scores["compound"] < 0


def test_score_result(analyzer):
    """Testa score_result em um resultado simulado."""
    result = SynthesizedResult(
        entity="Proj A",
        title="Incrível biblioteca de UI",
        description="Fácil de integrar e muito eficiente.",
        sources=["github"],
        urls=["http://example.com"],
        combined_score=90.0,
        metrics={},
        highlights=[],
        first_seen=None,
        last_seen=None,
    )
    scores = analyzer.score_result(result)
    assert scores["compound"] > 0  # Deve ser positivo


def test_score_neutrality(analyzer):
    """Verifica o score de neutralidade."""
    neutral_text = "O sistema foi implementado usando arquitetura de microsserviços e comunicação mTLS."
    biased_text = "Este é o melhor e mais incrível software do mundo, tudo é perfeito e maravilhoso!"
    
    neutrality_high = analyzer.score_neutrality(neutral_text)
    neutrality_low = analyzer.score_neutrality(biased_text)
    
    # O texto neutro deve ter um score de neutralidade mais alto
    assert neutrality_high > neutrality_low


def test_check_bias_positive(analyzer):
    """Verifica detecção de viés positivo."""
    results = [
        SynthesizedResult(
            entity="A", title="Excelente ferramenta", description="Muito rápida e incrível.",
            sources=["github"], urls=[], combined_score=80.0, metrics={}, highlights=[], first_seen=None, last_seen=None
        ),
        SynthesizedResult(
            entity="B", title="Ótimo plugin", description="Moderno e inovador.",
            sources=["github"], urls=[], combined_score=80.0, metrics={}, highlights=[], first_seen=None, last_seen=None
        ),
    ]
    bias = analyzer.check_bias(results)
    assert bias is not None
    assert "Viés Positivo" in bias


def test_check_bias_negative(analyzer):
    """Verifica detecção de viés negativo."""
    results = [
        SynthesizedResult(
            entity="A", title="Lento e cheio de bugs", description="Muito ruim e difícil de usar.",
            sources=["github"], urls=[], combined_score=80.0, metrics={}, highlights=[], first_seen=None, last_seen=None
        ),
        SynthesizedResult(
            entity="B", title="Inseguro e antigo", description="Cheio de erros e falhas.",
            sources=["github"], urls=[], combined_score=80.0, metrics={}, highlights=[], first_seen=None, last_seen=None
        ),
    ]
    bias = analyzer.check_bias(results)
    assert bias is not None
    assert "Viés Negativo" in bias


def test_check_bias_balanced(analyzer):
    """Verifica quando os resultados estão equilibrados."""
    results = [
        SynthesizedResult(
            entity="A", title="Excelente ferramenta", description="Muito rápida.",
            sources=["github"], urls=[], combined_score=80.0, metrics={}, highlights=[], first_seen=None, last_seen=None
        ),
        SynthesizedResult(
            entity="B", title="Lento e antigo", description="Cheio de erros.",
            sources=["github"], urls=[], combined_score=80.0, metrics={}, highlights=[], first_seen=None, last_seen=None
        ),
    ]
    bias = analyzer.check_bias(results)
    # Deve se anular ou ficar abaixo do threshold
    assert bias is None


def test_generate_sentiment_section(analyzer):
    """Garante que a seção Markdown é formatada corretamente."""
    results = [
        SynthesizedResult(
            entity="A",
            title="Projeto A",
            description="Criado com foco em performance excelente.",
            sources=["github"],
            urls=["http://example.com"],
            combined_score=75.0,
            metrics={},
            highlights=[],
            first_seen=None,
            last_seen=None,
        )
    ]
    markdown = analyzer.generate_sentiment_section(results)
    assert "## 🎭 Análise de Sentimento" in markdown
    assert "GitHub" in markdown
    assert "Índice de Neutralidade" in markdown
