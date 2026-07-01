import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from src.types import SearchResult
from src.link_verifier import LinkVerifier

def test_extract_links_cleaning():
    verifier = LinkVerifier()
    
    text = (
        "Check this out: https://example.com/page, it is great. "
        "Also see https://test.org/doc.pdf?v=1.0; and (https://another-one.com/index.html)."
    )
    
    links = verifier.extract_links(text)
    
    assert len(links) == 3
    assert "https://example.com/page" in links
    assert "https://test.org/doc.pdf?v=1.0" in links
    assert "https://another-one.com/index.html" in links
    # Deve garantir que a pontuação no final foi removida
    assert not any(l.endswith(",") or l.endswith(";") or l.endswith(")") for l in links)


@pytest.mark.asyncio
async def test_verify_url_success():
    verifier = LinkVerifier()
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    
    # Mock do AsyncClient do httpx
    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_client.head = AsyncMock(return_value=mock_response)
    
    url, is_alive, status, err = await verifier.verify_url(mock_client, "https://example.com")
    
    assert url == "https://example.com"
    assert is_alive is True
    assert status == 200
    assert err == ""


@pytest.mark.asyncio
async def test_verify_url_http_error_falls_back_to_get_success():
    verifier = LinkVerifier()
    
    # HEAD retorna 404
    mock_response_head = MagicMock()
    mock_response_head.status_code = 404
    
    # GET retorna 200
    mock_response_get = MagicMock()
    mock_response_get.status_code = 200
    
    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_client.head = AsyncMock(return_value=mock_response_head)
    mock_client.get = AsyncMock(return_value=mock_response_get)
    
    url, is_alive, status, err = await verifier.verify_url(mock_client, "https://example.com")
    
    assert is_alive is True
    assert status == 200
    # Deve ter tentado o HEAD e depois o GET
    mock_client.head.assert_called_once()
    mock_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_verify_results_penalizes_dead_links():
    verifier = LinkVerifier()
    
    r1 = SearchResult(
        source="web",
        title="Python Info",
        url="https://site.com/python",
        description="Great python info at https://realpython.com and broken link https://badsite.com/broken.",
        confidence_score=0.80
    )
    
    # Mock das respostas de validação: realpython.com=200, badsite.com/broken=404
    async def mock_verify_url(client, url):
        if "realpython.com" in url:
            return url, True, 200, ""
        else:
            return url, False, 404, "Not Found"
            
    verifier.verify_url = mock_verify_url
    
    verified_results = await verifier.verify_results([r1])
    
    assert len(verified_results) == 1
    result = verified_results[0]
    
    # Apenas o link vivo deve permanecer nas citações
    assert "https://realpython.com" in result.citations
    assert "https://badsite.com/broken" not in result.citations
    
    # Deve ter registrado o link quebrado nas métricas e flags
    assert "https://badsite.com/broken" in result.metrics["dead_links"]
    assert "dead_links_detected" in result.hallucination_flags
    
    # Deve ter penalizado o score
    assert result.confidence_score < 0.80
