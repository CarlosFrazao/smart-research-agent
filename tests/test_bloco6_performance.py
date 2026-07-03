import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.utils.rate_limiter import TokenBucket, DomainRateLimiter
from src.search.semantic_reranker import SemanticReranker
from src.search.serpapi_searcher import SerpAPISearcher
from src.services.reasoning_service import ReasoningService
from src.services.search_service import SearchService
from src.types import ExpandedQuery, IntentResult
from src.operation_modes import OperationConfig

# ---------------------------------------------------------------------------
# 1. Tests for AdaptiveRateLimiter
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_adaptive_rate_limiter_backoff():
    # Inicializa com rate=2.0 req/s
    bucket = TokenBucket(rate=2.0, capacity=2)
    assert bucket.rate == 2.0

    # Recebe 429 ou 403 -> deve reduzir a taxa pela metade
    bucket.record(429)
    assert bucket.rate == 1.0

    bucket.record(403)
    assert bucket.rate == 0.5

    # Continua diminuindo até o mínimo de 0.1 req/s
    for _ in range(5):
        bucket.record(429)
    assert bucket.rate == 0.1


@pytest.mark.anyio
async def test_adaptive_rate_limiter_recovery():
    bucket = TokenBucket(rate=1.0, capacity=2)
    bucket.initial_rate = 2.0  # Permite recuperação até 2.0

    # Registra 9 sucessos -> ainda não deve recuperar
    for _ in range(9):
        bucket.record(200)
    assert bucket.rate == 1.0

    # Registra o 10º sucesso -> deve aumentar 10% (1.0 * 1.1 = 1.1)
    bucket.record(200)
    assert bucket.rate == 1.1

    # Recuperação é limitada a initial_rate (2.0)
    bucket.rate = 1.95
    bucket._success_streak = 9
    bucket.record(200)
    assert bucket.rate == 2.0  # Teto respeitado


@pytest.mark.anyio
async def test_domain_rate_limiter_record():
    DomainRateLimiter.reset_all()
    url = "https://example.com/api/test"

    # Inicialmente, o bucket é criado sob demanda
    bucket = DomainRateLimiter._get_bucket("example.com")
    initial = bucket.rate

    # Notifica erro 429
    DomainRateLimiter.record(url, 429)
    assert bucket.rate == initial / 2

    # Notifica sucessos
    for _ in range(10):
        DomainRateLimiter.record(url, 200)
    assert bucket.rate == min(bucket.initial_rate, (initial / 2) * 1.1)


# ---------------------------------------------------------------------------
# 2. Tests for SemanticReranker
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_keyword_score_and_fallback_reranker():
    results = [
        {
            "title": "Outro assunto completamente diferente",
            "snippet": "Nada a ver com Python",
            "url": "http://other.com",
        },
        {
            "title": "Python Programming",
            "snippet": "Learn python programming, loops, classes",
            "url": "http://python.org",
        },
    ]
    query = "Python programming loops"

    # Executa o re-ranking usando o fallback (keyword-overlap)
    reranker = SemanticReranker()
    # Força modelo indisponível para garantir o fluxo do fallback
    reranker._model_available = False

    reranked = await reranker.rerank(query, results)
    assert len(reranked) == 2
    # Python programming deve vir primeiro devido ao overlap de keywords
    assert "Python" in reranked[0]["title"]
    assert reranked[0]["_semantic_score"] is None


@pytest.mark.anyio
async def test_semantic_rerank_with_sentence_transformers():
    # Verifica se o sentence-transformers está instalado
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        pytest.skip("sentence-transformers não disponível para este teste")

    results = [
        {
            "title": "Outro assunto",
            "snippet": "Abacaxis e laranjas no pomar",
            "url": "http://fruits.com",
        },
        {
            "title": "Desenvolvimento Web",
            "snippet": "Criando aplicações modernas com React, HTML e CSS",
            "url": "http://react.dev",
        },
    ]
    query = "Como criar um site com React e CSS"

    reranker = SemanticReranker()
    reranked = await reranker.rerank(query, results)
    assert len(reranked) == 2
    # Desenvolvimento Web tem maior similaridade semântica com a query do que "Outro assunto"
    assert "Web" in reranked[0]["title"]
    assert reranked[0]["_semantic_score"] is not None
    assert reranked[0]["_semantic_score"] > 0.0


@pytest.mark.anyio
async def test_reasoning_service_rank_integration():
    mock_orch = MagicMock()
    mock_orch.semantic_reranker = SemanticReranker()
    mock_orch.semantic_reranker._model_available = False  # Força fallback

    # Mock do ranker clássico (retorna na mesma ordem)
    mock_ranker = AsyncMock()
    mock_ranker.rank = AsyncMock(side_effect=lambda x: x)
    mock_orch.ranker = mock_ranker

    svc = ReasoningService(mock_orch)

    # Resultados brutos como objetos fictícios
    class MockResult:
        def __init__(self, title, snippet, url):
            self.title = title
            self.snippet = snippet
            self.url = url
            self.confidence_score = 0.8

    results = [
        MockResult("Banana", "Como descascar banana", "http://banana.com"),
        MockResult(
            "Python", "Escrever código Python de alta performance", "http://python.org"
        ),
    ]

    # Executa com query
    ranked = await svc.rank(results, query="Python de alta performance")
    # O de Python deve vir primeiro por causa do reranking semântico de fallback
    assert len(ranked) == 2
    assert "Python" in ranked[0].title


# ---------------------------------------------------------------------------
# 3. Tests for SerpAPISearcher & Fallback in SearchService
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_serpapi_searcher_disabled_or_missing_key():
    searcher = SerpAPISearcher(api_key="")
    assert not searcher.is_available
    res = await searcher.search("test")
    assert res == []


@pytest.mark.anyio
async def test_serpapi_searcher_mocked_results():
    with patch(
        "src.search.serpapi_searcher._SerpAPIGoogleSearch"
    ) as mock_search_class, patch(
        "src.search.serpapi_searcher._SERPAPI_AVAILABLE", True
    ):
        mock_instance = MagicMock()
        mock_instance.get_dict.return_value = {
            "organic_results": [
                {
                    "title": "Resultado 1",
                    "link": "http://res1.com",
                    "snippet": "Snippet 1",
                    "position": 1,
                },
                {
                    "title": "Resultado 2",
                    "link": "http://res2.com",
                    "snippet": "Snippet 2",
                    "position": 2,
                },
            ]
        }
        mock_search_class.return_value = mock_instance

        searcher = SerpAPISearcher(api_key="mock_key")
        assert searcher.is_available

        res = await searcher.search("Python")
        assert len(res) == 2
        assert res[0]["title"] == "Resultado 1"
        assert res[0]["url"] == "http://res1.com"
        assert res[0]["source"] == "serpapi"


@pytest.mark.anyio
async def test_search_service_serpapi_fallback():
    mock_orch = MagicMock()
    mock_orch.config.timeout_per_source = 10
    mock_orch.config.max_results_per_source = 5
    mock_orch.config.serpapi_api_key = "mock_key"
    mock_orch.config.serpapi_enabled = True

    # Mock do cache
    mock_cache = MagicMock()
    mock_cache.get = AsyncMock(return_value=None)
    mock_cache.set = AsyncMock()
    mock_orch.cache = mock_cache

    # Mock dos searchers disponíveis
    mock_searcher_fail = AsyncMock()
    mock_searcher_fail.enabled = True
    mock_searcher_fail.timeout = 10
    mock_searcher_fail.search = AsyncMock(side_effect=Exception("Connection refused"))

    mock_serpapi = AsyncMock()
    mock_serpapi.is_available = True
    mock_serpapi.search = AsyncMock(
        return_value=[
            {
                "title": "SerpAPI Result",
                "url": "http://serpapi.com",
                "snippet": "SerpAPI Snippet",
            }
        ]
    )

    mock_orch.searchers = {"searxng": mock_searcher_fail, "serpapi": mock_serpapi}

    # Define o modo de operação do mock
    mode_config = OperationConfig(
        name="test_mode",
        description="test",
        searchers=["searxng", "serpapi"],
        scrapers=[],
        confidence_threshold=0.5,
        max_depth=1,
        enable_auditor=False,
        enable_race=False,
        proxy_strategy="static",
        cache_strategy="none",
        timeout_seconds=10,
        cost_optimization=True,
    )
    mock_orch.operation_mode = mode_config

    svc = SearchService(mock_orch)

    # Plano fictício
    class MockPlan:
        def __init__(self):
            self.sources = {
                "searxng": [
                    ExpandedQuery(
                        query="Python info",
                        type="general",
                        priority="alta",
                        rationale="test",
                    )
                ]
            }

    intent = IntentResult(
        domain=MagicMock(),
        entities=[],
        intention=MagicMock(),
        urgency="nao",
        confidence="alta",
    )
    intent.domain.value = "general"

    # Executa a busca. O searxng vai falhar, results ficará vazio,
    # então deve ativar o fallback do SerpAPI.
    results = await svc.execute(
        [
            ExpandedQuery(
                query="Python info", type="general", priority="alta", rationale="test"
            )
        ],
        MockPlan(),
        intent,
    )

    assert len(results) == 1
    assert results[0].source == "serpapi"
    assert results[0].url == "http://serpapi.com"
    assert results[0].title == "SerpAPI Result"
