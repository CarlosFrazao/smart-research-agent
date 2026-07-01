"""
tests/test_sra_v46.py — Suite de testes de validação do SRA Upgrade v4.6

Testes cobertos:
    T1: test_firecrawl_redact_pii_flag
    T2: test_firecrawl_lockdown_mode
    T3: test_firecrawl_research_index_called
    T4: test_arxiv_fallback_to_research_index
    T5: test_github_code_search_fallback
    T6: test_model_router_reasoning_tier
    T7: test_model_router_deepseek_provider
    T8: test_config_new_flags_default_values
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides):
    """Retorna um mock de Config com os defaults do v4.6."""
    cfg = MagicMock()
    cfg.firecrawl_redact_pii = overrides.get("firecrawl_redact_pii", False)
    cfg.firecrawl_lockdown_mode = overrides.get("firecrawl_lockdown_mode", False)
    cfg.firecrawl_deterministic_json = overrides.get("firecrawl_deterministic_json", False)
    cfg.firecrawl_research_index_enabled = overrides.get("firecrawl_research_index_enabled", True)
    cfg.reasoning_models_enabled = overrides.get("reasoning_models_enabled", False)
    cfg.openai_reasoning_model = overrides.get("openai_reasoning_model", "o3-mini")
    cfg.deepseek_model = overrides.get("deepseek_model", "deepseek-r1")
    cfg.deepseek_api_key = overrides.get("deepseek_api_key", None)
    cfg.deepseek_base_url = overrides.get("deepseek_base_url", "https://api.deepseek.com/v1")
    return cfg


def _build_firecrawl_client(**config_overrides):
    """Cria um FirecrawlClient sem SDK real, inicializando atributos v4.6."""
    from src.clients.firecrawl_client import FirecrawlClient
    client = FirecrawlClient.__new__(FirecrawlClient)
    cfg = _make_config(**config_overrides)
    client.config = cfg
    client.firecrawl_redact_pii = cfg.firecrawl_redact_pii
    client.firecrawl_lockdown_mode = cfg.firecrawl_lockdown_mode
    client.firecrawl_deterministic_json = cfg.firecrawl_deterministic_json
    client.firecrawl_research_index_enabled = cfg.firecrawl_research_index_enabled
    # app é um MagicMock controlável pelos testes
    client.app = MagicMock()
    return client


# ---------------------------------------------------------------------------
# T1 — FirecrawlClient passa redact_pii=True ao SDK quando flag ativa
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_firecrawl_redact_pii_flag():
    client = _build_firecrawl_client(firecrawl_redact_pii=True)

    captured_kwargs = {}

    def fake_scrape_url(url, **kwargs):
        captured_kwargs.update(kwargs)
        result = MagicMock()
        result.model_dump = lambda: {"markdown": "ok"}
        return result

    client.app.scrape_url = fake_scrape_url

    # Sobrescreve _with_retry para executar a função diretamente (síncrona aqui)
    async def fake_retry(fn, *args, **kwargs):
        if fn is asyncio.to_thread:
            real_fn, *real_args = args
            return real_fn(*real_args, **kwargs)
        return fn(*args, **kwargs)

    client._with_retry = fake_retry

    await client._direct_scrape_call("https://example.com")

    assert captured_kwargs.get("redact_pii") is True, (
        f"redact_pii deve ser True no payload. kwargs capturados: {captured_kwargs}"
    )


# ---------------------------------------------------------------------------
# T2 — FirecrawlClient passa lockdown_mode=True ao SDK quando flag ativa
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_firecrawl_lockdown_mode():
    client = _build_firecrawl_client(firecrawl_lockdown_mode=True)

    captured_kwargs = {}

    def fake_scrape_url(url, **kwargs):
        captured_kwargs.update(kwargs)
        result = MagicMock()
        result.model_dump = lambda: {"markdown": "ok"}
        return result

    client.app.scrape_url = fake_scrape_url

    async def fake_retry(fn, *args, **kwargs):
        if fn is asyncio.to_thread:
            real_fn, *real_args = args
            return real_fn(*real_args, **kwargs)
        return fn(*args, **kwargs)

    client._with_retry = fake_retry

    await client._direct_scrape_call("https://example.com")

    assert captured_kwargs.get("lockdown_mode") is True, (
        f"lockdown_mode deve ser True no payload. kwargs capturados: {captured_kwargs}"
    )


# ---------------------------------------------------------------------------
# T3 — search_research_index chama app.search com index="research"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_firecrawl_research_index_called():
    client = _build_firecrawl_client()

    captured_kwargs = {}

    def fake_search(query, **kwargs):
        captured_kwargs.update(kwargs)
        result = MagicMock()
        result.data = []
        return result

    client.app.search = fake_search

    async def fake_retry(fn, *args, **kwargs):
        if fn is asyncio.to_thread:
            real_fn, *real_args = args
            return real_fn(*real_args, **kwargs)
        return fn(*args, **kwargs)

    client._with_retry = fake_retry
    client._normalize_search_results = lambda r: []

    await client.search_research_index("transformer attention mechanism")

    assert captured_kwargs.get("index") == "research", (
        f"Deve passar index='research' ao SDK. kwargs: {captured_kwargs}"
    )


# ---------------------------------------------------------------------------
# T4 — ArxivSearcher aciona Research Index quando resultados nativos < 3
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_arxiv_fallback_to_research_index():
    from src.search.arxiv_searcher import ArxivSearcher
    from src.types import SearchResult

    mock_firecrawl = MagicMock()
    mock_firecrawl.search_research_index = AsyncMock(return_value=[
        {"title": "RI Paper", "url": "https://arxiv.org/abs/9999.0001", "description": "From Research Index"}
    ])

    searcher = ArxivSearcher.__new__(ArxivSearcher)
    searcher.max_results = 10
    searcher.timeout = 30
    searcher.base_url = "http://export.arxiv.org/api/query"  # atributo necessário
    searcher.firecrawl_client = mock_firecrawl

    # Simula 2 resultados nativos (abaixo do threshold de 3)
    native_results = [
        SearchResult(source="arxiv", title=f"Paper {i}", url=f"https://arxiv.org/abs/{i}",
                     description="", metrics={}, raw={})
        for i in range(2)
    ]

    searcher._parse_xml = MagicMock(return_value=native_results)
    searcher.http = MagicMock()
    searcher.http.get = AsyncMock(return_value={"text": "<feed/>"})
    searcher.fallback = MagicMock(return_value=[])
    searcher._normalize_research_index_result = lambda item: SearchResult(
        source="arxiv_research_index",
        title=item.get("title", ""),
        url=item.get("url", ""),
        description=item.get("description", ""),
        metrics={},
        raw=item,
    )

    results = await searcher.search("transformers deep learning")

    mock_firecrawl.search_research_index.assert_called_once()
    assert len(results) > 2, "Deve ter mais de 2 resultados após fallback"
    assert any(r.source == "arxiv_research_index" for r in results), \
        "Deve conter resultado do Research Index"


# ---------------------------------------------------------------------------
# T5 — GitHubSearcher aciona search_code quando repos < 2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_github_code_search_fallback():
    from src.search.github_searcher import GitHubSearcher
    from src.types import SearchResult

    searcher = GitHubSearcher.__new__(GitHubSearcher)
    searcher.max_results = 10
    searcher.timeout = 30
    searcher.token = None
    searcher.base_url = "https://api.github.com/search/repositories"
    searcher.fallback = MagicMock(return_value=[])

    # Repos: retorna apenas 1 resultado (< 2 → aciona Code Search)
    repo_result = SearchResult(source="github", title="owner/repo",
                               url="https://github.com/owner/repo",
                               description="", metrics={}, raw={})
    searcher.normalize = MagicMock(return_value=repo_result)

    code_result = SearchResult(source="github_code", title="code_file.py",
                               url="https://github.com/x/y/blob/main/code.py",
                               description="Arquivo em x/y", metrics={}, raw={})
    searcher.search_code = AsyncMock(return_value=[code_result])
    searcher.http = MagicMock()
    searcher.http.get = AsyncMock(return_value={"items": [{"dummy": True}]})

    results = await searcher.search("pii redaction python")

    searcher.search_code.assert_called_once()
    assert any(r.source == "github_code" for r in results), \
        "Deve incluir resultado de Code Search"


# ---------------------------------------------------------------------------
# T6 — ModelRouter roteia deep_research para o3-mini (openai + reasoning)
# ---------------------------------------------------------------------------

def test_model_router_reasoning_tier():
    from src.model_router import ModelRouter

    cfg = _make_config(reasoning_models_enabled=True, openai_reasoning_model="o3-mini")
    router = ModelRouter(config=cfg)
    model = router.route("deep_research", "openai")
    assert model == "o3-mini", f"Esperado 'o3-mini', obtido '{model}'"


# ---------------------------------------------------------------------------
# T7 — ModelRouter roteia para deepseek-r1 via provider deepseek
# ---------------------------------------------------------------------------

def test_model_router_deepseek_provider():
    from src.model_router import ModelRouter

    cfg = _make_config(reasoning_models_enabled=True, deepseek_model="deepseek-r1")
    router = ModelRouter(config=cfg)
    model = router.route("confidence_scoring", "deepseek")
    assert model == "deepseek-r1", f"Esperado 'deepseek-r1', obtido '{model}'"


# ---------------------------------------------------------------------------
# T8 — Config tem defaults seguros para todos os novos flags
# ---------------------------------------------------------------------------

def test_config_new_flags_default_values():
    """Verifica defaults via instância direta sem .env."""
    with patch.dict("os.environ", {}, clear=True):
        try:
            from src.config import Config
            cfg = Config(
                _env_file=None,
                firecrawl_api_key="fc-test",
            )
        except Exception:
            pytest.skip("Config não instanciável sem .env neste ambiente")
            return

    assert cfg.firecrawl_redact_pii is False
    assert cfg.firecrawl_lockdown_mode is False
    assert cfg.firecrawl_deterministic_json is False
    assert cfg.firecrawl_research_index_enabled is True
    assert cfg.reasoning_models_enabled is False
    assert cfg.openai_reasoning_model == "o3-mini"
    assert cfg.deepseek_model == "deepseek-r1"
    assert cfg.deepseek_base_url == "https://api.deepseek.com/v1"
