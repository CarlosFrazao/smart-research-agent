"""
Testes do ClinicalTrialsSearcher (Bloco 8 — E3-T3)

Valida:
1. Busca bem-sucedida retorna SearchResult com nctId, url, e campos essenciais normalizados.
2. Estudo sem briefTitle é descartado (normalize retorna None).
3. Fallback WebSearcher é acionado quando API retorna < 2 resultados.
4. Registro na SearcherFactory (factory sempre registra 'clinicaltrials').
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.search.clinicaltrials_searcher import ClinicalTrialsSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient


def _make_searcher(config: dict | None = None) -> ClinicalTrialsSearcher:
    return ClinicalTrialsSearcher(config or {"timeout": 10, "max_results": 10})


def _mock_http(searcher: ClinicalTrialsSearcher, payload: dict) -> MagicMock:
    http_mock = MagicMock(spec=HTTPClient)
    http_mock.get = AsyncMock(return_value=payload)
    searcher.http = http_mock
    return http_mock


# Resposta simulada da API v2 (com estrutura completa de protocolSection)
_STUDIES_OK = {
    "studies": [
        {
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT03380325",
                    "briefTitle": "Iloprost on Capillary Recruitment in Type 2 Diabetes",
                },
                "statusModule": {
                    "overallStatus": "COMPLETED",
                    "startDateStruct": {"date": "2016-05-11", "type": "ACTUAL"},
                },
                "conditionsModule": {
                    "conditions": ["Diabetes Mellitus, Type 2", "Insulin Sensitivity/Resistance"]
                },
                "designModule": {"phases": ["NA"]},
            }
        },
        {
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT04233446",
                    "briefTitle": "Metformin and Cognitive Decline in Prediabetes",
                },
                "statusModule": {
                    "overallStatus": "RECRUITING",
                    "startDateStruct": {"date": "2020-02-01", "type": "ACTUAL"},
                },
                "conditionsModule": {"conditions": ["Prediabetes", "Cognitive Dysfunction"]},
                "designModule": {"phases": ["PHASE2"]},
            }
        },
    ]
}

_STUDIES_EMPTY = {"studies": []}


@pytest.mark.asyncio
async def test_clinicaltrials_search_success():
    searcher = _make_searcher()
    _mock_http(searcher, _STUDIES_OK)

    results = await searcher.search("type 2 diabetes capillary recruitment")

    assert len(results) == 2
    r1 = results[0]
    assert r1.source == "clinicaltrials"
    assert r1.title == "Iloprost on Capillary Recruitment in Type 2 Diabetes"
    assert r1.metrics["nct_id"] == "NCT03380325"
    assert r1.url == "https://clinicaltrials.gov/study/NCT03380325"
    assert r1.metrics["overall_status"] == "COMPLETED"
    assert r1.metrics["phase"] == "NA"
    assert "Diabetes Mellitus, Type 2" in r1.description
    assert r1.metrics["start_date"] == "2016-05-11"

    r2 = results[1]
    assert r2.metrics["nct_id"] == "NCT04233446"
    assert r2.metrics["phase"] == "PHASE2"
    assert r2.metrics["overall_status"] == "RECRUITING"


@pytest.mark.asyncio
async def test_clinicaltrials_discards_study_without_title():
    """Estudo sem briefTitle deve ser descartado (normalize -> None)."""
    payload = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT00000001"},
                    # sem briefTitle
                    "statusModule": {"overallStatus": "UNKNOWN"},
                }
            }
        ]
    }
    searcher = _make_searcher()
    _mock_http(searcher, payload)

    results = await searcher.search("missing title study")
    assert len(results) == 0  # descartado silenciosamente


@pytest.mark.asyncio
async def test_clinicaltrials_web_fallback_on_empty():
    searcher = _make_searcher()
    _mock_http(searcher, _STUDIES_EMPTY)

    web_mock = MagicMock()
    web_mock.enabled = True
    web_mock.search = AsyncMock(
        return_value=[
            SearchResult(
                source="web",
                title="Web CT Fallback",
                url="http://web.example/ct",
                description="Fallback desc",
            )
        ]
    )
    searcher.web_fallback = web_mock

    results = await searcher.search("rare disease trial")

    assert len(results) == 1
    assert results[0].source == "web"
    assert results[0].title == "Web CT Fallback"
    web_mock.search.assert_called_once()


def test_clinicaltrials_registered_in_factory():
    """SearcherFactory sempre registra 'clinicaltrials' (sem API key)."""
    from src.search.factory import SearcherFactory

    orchestrator = MagicMock()
    orchestrator.config.timeout_per_source = 30
    orchestrator.config.max_results_per_source = 10
    orchestrator.config.github_token = None
    orchestrator.config.producthunt_token = None
    orchestrator.config.firecrawl_api_key = None
    orchestrator.config.firecrawl_base_url = None
    orchestrator.config.spider_api_key = None
    orchestrator.config.spider_base_url = None
    orchestrator.config.steel_api_key = None
    orchestrator.config.steel_base_url = None

    searchers = SearcherFactory.create_searchers(orchestrator)
    assert "clinicaltrials" in searchers
    assert isinstance(searchers["clinicaltrials"], ClinicalTrialsSearcher)


def test_clinicaltrials_in_available_searchers():
    from src.search.factory import SearcherFactory

    assert "clinicaltrials" in SearcherFactory.get_available_searchers()
