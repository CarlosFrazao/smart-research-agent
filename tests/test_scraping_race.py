import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from src.clients.scraping_race_client import ScrapingRaceClient

@pytest.mark.asyncio
async def test_scraping_race_firecrawl_wins():
    # Mock do FirecrawlClient que responde rápido
    firecrawl_mock = MagicMock()
    firecrawl_mock._direct_scrape_call = AsyncMock(return_value={
        "success": True,
        "markdown": "Este é o conteúdo retornado pelo Firecrawl." + " A" * 150
    })
    
    client = ScrapingRaceClient(firecrawl_mock)
    
    # Mock do HTTP direct que responde lento de propósito
    async def mock_direct_http(url):
        await asyncio.sleep(2.0)
        return {"success": True, "markdown": "Lento demais"}
        
    client._scrape_direct_http = mock_direct_http
    
    # Executa o scrape
    res = await client.scrape("https://example.com")
    
    assert res["success"] is True
    assert "Firecrawl" in res["markdown"]
    assert res["metadata"]["engine"] == "firecrawl"


@pytest.mark.asyncio
async def test_scraping_race_direct_http_wins():
    # Mock do FirecrawlClient que responde lento
    firecrawl_mock = MagicMock()
    async def mock_firecrawl(url, formats=None):
        await asyncio.sleep(2.0)
        return {"success": True, "markdown": "Lento demais do Firecrawl"}
    firecrawl_mock._direct_scrape_call = mock_firecrawl
    
    client = ScrapingRaceClient(firecrawl_mock)
    
    # Mock do HTTP direct que responde instantâneo
    async def mock_direct_http(url):
        return {"success": True, "markdown": "# Título\nEste é o conteúdo rápido via direct HTTP." + " B" * 150}
        
    client._scrape_direct_http = mock_direct_http
    
    # Executa o scrape
    res = await client.scrape("https://example.com")
    
    assert res["success"] is True
    assert "rápido" in res["markdown"]
    assert res["metadata"]["engine"] == "direct_http"


@pytest.mark.asyncio
async def test_scraping_race_both_failed_fallback_succeeds():
    firecrawl_mock = MagicMock()
    # Scrape normal falha
    firecrawl_mock._direct_scrape_call = AsyncMock(side_effect=[
        Exception("Erro de conexão no container"), # Primeiro erro na corrida
        {"success": True, "markdown": "Conteúdo recuperado pelo Fallback final" + " C" * 150} # Sucesso no Fallback final
    ])
    
    client = ScrapingRaceClient(firecrawl_mock)
    
    # Direct HTTP também falha
    client._scrape_direct_http = AsyncMock(return_value={"success": False})
    
    # Executa
    res = await client.scrape("https://example.com")
    
    assert res["success"] is True
    assert "Fallback" in res["markdown"]
    assert res["metadata"]["engine"] == "firecrawl_fallback"
