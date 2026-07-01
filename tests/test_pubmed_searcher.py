import pytest
from unittest.mock import AsyncMock, MagicMock
from src.search.pubmed_searcher import PubMedSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient


@pytest.mark.asyncio
async def test_pubmed_search_success():
    # 1. Configura mock do HTTPClient
    http_mock = MagicMock(spec=HTTPClient)
    
    # Mock do ESearch
    esearch_data = {
        "json": {
            "esearchresult": {
                "idlist": ["123456", "789012"]
            }
        }
    }
    # Mock do ESummary
    esummary_data = {
        "json": {
            "result": {
                "123456": {
                    "title": "Artificial Intelligence in Medicine",
                    "pubdate": "2024 Jan 1",
                    "source": "J Med AI",
                    "authors": [
                        {"name": "Smith J"},
                        {"name": "Doe J"}
                    ],
                    "articleids": [
                        {"idtype": "doi", "value": "10.1000/xyz123"}
                    ]
                },
                "789012": {
                    "title": "Machine Learning Diagnostic Tool",
                    "pubdate": "2024 Feb 15",
                    "source": "Diagnostics",
                    "authors": [
                        {"name": "Alice A"},
                        {"name": "Bob B"},
                        {"name": "Charlie C"},
                        {"name": "David D"}
                    ],
                    "articleids": []
                }
            }
        }
    }
    
    # Configura o mock do HTTPClient para retornar sequencialmente
    http_mock.get = AsyncMock(side_effect=[esearch_data, esummary_data])
    
    # 2. Instancia o PubMedSearcher com o mock
    config = {"timeout": 10, "max_results": 5}
    searcher = PubMedSearcher(config)
    searcher.http = http_mock
    
    results = await searcher.search("medical AI")
    
    # 3. Asserções
    assert len(results) == 2
    
    # Primeiro resultado
    r1 = results[0]
    assert r1.source == "pubmed"
    assert r1.title == "Artificial Intelligence in Medicine"
    assert r1.url == "https://pubmed.ncbi.nlm.nih.gov/123456/"
    assert "J Med AI" in r1.description
    assert "Smith J, Doe J" in r1.description
    assert "10.1000/xyz123" in r1.description
    assert r1.metrics["pmid"] == "123456"
    assert r1.metrics["doi"] == "10.1000/xyz123"
    
    # Segundo resultado (mais de 3 autores - deve ter et al.)
    r2 = results[1]
    assert "Alice A, Bob B, Charlie C et al." in r2.description
    assert r2.metrics["doi"] == ""


@pytest.mark.asyncio
async def test_pubmed_fallback_triggered_on_empty():
    http_mock = MagicMock(spec=HTTPClient)
    # ESearch retorna lista vazia
    esearch_empty = {"json": {"esearchresult": {"idlist": []}}}
    http_mock.get = AsyncMock(return_value=esearch_empty)
    
    # Mock do WebSearcher fallback
    web_mock = MagicMock()
    web_mock.enabled = True
    web_mock.search = AsyncMock(return_value=[
        SearchResult(source="web", title="Web PubMed Fallback", url="http://web.com", description="Fallback desc")
    ])
    
    config = {"timeout": 10, "max_results": 5}
    searcher = PubMedSearcher(config)
    searcher.http = http_mock
    searcher.web_fallback = web_mock
    
    results = await searcher.search("rare disease study")
    
    assert len(results) == 1
    assert results[0].source == "web"
    assert results[0].title == "Web PubMed Fallback"
    web_mock.search.assert_called_once_with("PubMed article rare disease study")
