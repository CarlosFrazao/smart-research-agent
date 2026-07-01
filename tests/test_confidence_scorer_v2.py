import pytest
from datetime import datetime
from src.types import SearchResult
from src.confidence_scorer_v2 import ConfidenceScorerV2

@pytest.mark.asyncio
async def test_classify_claim_heuristics():
    scorer = ConfidenceScorerV2()
    
    # Teste de Estatística
    type_stat, _ = scorer._classify_claim(
        "Based on a recent survey, 84.5% of developers prefer Python over Java, showing a huge difference.", 
        "Python vs Java Survey"
    )
    assert type_stat == "statistics"
    
    # Teste de Fato
    type_fact, _ = scorer._classify_claim(
        "According to official docs, Python was released in 1991 by Guido van Rossum.", 
        "Python History"
    )
    assert type_fact == "fact"
    
    # Teste de Opinião
    type_opinion, _ = scorer._classify_claim(
        "In my opinion, Python is the most beautiful language ever created, I believe everyone should use it.", 
        "My thoughts on Python"
    )
    assert type_opinion == "opinion"


@pytest.mark.asyncio
async def test_calculate_freshness():
    scorer = ConfidenceScorerV2()
    current_year = datetime.now().year
    
    # Teste com ano recente (atual)
    score_recent, year_recent = scorer._calculate_freshness(f"This library was updated in {current_year}.")
    assert year_recent == current_year
    assert score_recent == 1.0
    
    # Teste com ano antigo
    score_old, year_old = scorer._calculate_freshness("This article was written in 2012.")
    assert year_old == 2012
    assert score_old < 0.40  # Bastante antigo, deve ser penalizado


@pytest.mark.asyncio
async def test_detect_link_circularity():
    scorer = ConfidenceScorerV2()
    
    # Cria dois resultados que citam um ao outro
    r1 = SearchResult(
        source="web",
        title="Article A",
        url="https://site-a.com/article",
        description="We agree with the points made in https://site-b.com/article about this topic."
    )
    
    r2 = SearchResult(
        source="web",
        title="Article B",
        url="https://site-b.com/article",
        description="As stated by https://site-a.com/article, this is the best approach."
    )
    
    results = [r1, r2]
    circular_map = scorer._detect_link_circularity(results)
    
    assert "https://site-a.com/article" in circular_map
    assert "https://site-b.com/article" in circular_map
    assert circular_map["https://site-a.com/article"] == ["https://site-b.com/article"]


@pytest.mark.asyncio
async def test_score_batch_with_v2_features():
    scorer = ConfidenceScorerV2()
    
    # Article A (Cita B e tem opinião)
    r1 = SearchResult(
        source="web",
        title="Article A",
        url="https://site-a.com/article",
        description="In my opinion, this library is terrible. See: https://site-b.com/article"
    )
    
    # Article B (Cita A e tem estatística atual)
    current_year = datetime.now().year
    r2 = SearchResult(
        source="web",
        title="Article B",
        url="https://site-b.com/article",
        description=f"85% of users reported success in {current_year}. Source: https://site-a.com/article"
    )
    
    scored_results = await scorer.score_batch([r1, r2], detect_circularity=True)
    
    assert len(scored_results) == 2
    
    # Validar que a circularidade foi registrada
    assert "circular_reference" in scored_results[0].hallucination_flags
    assert "circular_reference" in scored_results[1].hallucination_flags
    
    # Validar claims
    assert scored_results[0].metrics["claim_type"] == "opinion"
    assert scored_results[1].metrics["claim_type"] == "statistics"
