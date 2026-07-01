"""
Testes unitários da Fase 4 — Autonomia
Cobre: OperationModes, ResearchAuditor, HealthMonitor
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ─── OperationModes ───────────────────────────────────────────────────────────

from src.operation_modes import OperationModes, OperationConfig


def test_operation_modes_all_seven_exist():
    """Verifica que os 7 modos estão registrados."""
    modes = OperationModes.list_modes()
    assert len(modes) == 7
    expected = {"guerrilha", "cirurgia", "radar", "arqueologia", "concorrencia", "black_ops", "debate"}
    assert set(modes) == expected


def test_operation_modes_get_valid():
    """Retorna config correta para modo válido."""
    config = OperationModes.get_mode("guerrilha")
    assert isinstance(config, OperationConfig)
    assert config.name == "guerrilha"
    assert config.max_depth == 1
    assert config.enable_auditor is False
    assert config.cost_optimization is True


def test_operation_modes_fallback_on_unknown():
    """Retorna modo padrão (cirurgia) para modo desconhecido."""
    config = OperationModes.get_mode("inexistente_xyz")
    assert config.name == OperationModes.DEFAULT_MODE


def test_operation_modes_black_ops_is_max():
    """black_ops deve ter maior max_depth e maior confidence_threshold."""
    black_ops = OperationModes.get_mode("black_ops")
    for name in OperationModes.list_modes():
        other = OperationModes.get_mode(name)
        if name != "black_ops":
            assert black_ops.max_depth >= other.max_depth


def test_operation_modes_auto_select():
    """auto_select identifica o modo correto por palavras-chave (ASCII e Unicode)."""
    assert OperationModes.auto_select("pesquisa rapida sobre Python") == "guerrilha"
    assert OperationModes.auto_select("verificar claim sobre IA") == "cirurgia"
    assert OperationModes.auto_select("ultimas novidades em LLMs") == "radar"
    assert OperationModes.auto_select("historico legado do Python 2") == "arqueologia"
    assert OperationModes.auto_select("concorrente do Cursor IDE") == "concorrencia"
    assert OperationModes.auto_select("pesquisa completa sobre deep learning") == "black_ops"


def test_operation_modes_to_dict():
    """to_dict retorna todos os campos esperados."""
    config = OperationModes.get_mode("radar")
    d = config.to_dict()
    assert "name" in d
    assert "confidence_threshold" in d
    assert "searchers" in d
    assert isinstance(d["searchers"], list)


# ─── ResearchAuditor ─────────────────────────────────────────────────────────

from src.research_auditor import ResearchAuditor, AuditClaim


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.generate_structured = AsyncMock(return_value=[
        "Python 3.13 supports GIL-free execution.",
        "The latest version of FastAPI is 0.115.",
        "ChromaDB supports cosine distance natively.",
    ])
    return llm


@pytest.fixture
def mock_results():
    """Resultados simulados com conteúdo relevante."""
    results = []
    for title, desc in [
        ("Python 3.13 Release Notes", "Python 3.13 supports free-threaded mode and GIL-free execution natively."),
        ("FastAPI Changelog", "FastAPI 0.115 was released with improved async support and OpenAPI 3.1."),
        ("ChromaDB Docs", "ChromaDB supports cosine, l2, and IP distance metrics for vector search."),
    ]:
        r = MagicMock()
        r.title = title
        r.description = desc
        r.url = f"https://example.com/{title.replace(' ', '-').lower()}"
        r.confidence_score = 0.85
        results.append(r)
    return results


@pytest.mark.asyncio
async def test_auditor_extracts_claims(mock_llm):
    """Verifica que claims são extraídas via LLM."""
    auditor = ResearchAuditor(llm_client=mock_llm)
    claims = await auditor._extract_claims("Some research report text.")
    assert len(claims) == 3
    assert all(isinstance(c, AuditClaim) for c in claims)


@pytest.mark.asyncio
async def test_auditor_validates_high_coverage(mock_llm, mock_results):
    """Claims com alta cobertura devem ser marcadas como 'verified'."""
    auditor = ResearchAuditor(llm_client=mock_llm)
    claims = [
        AuditClaim(text="Python supports free-threaded execution."),
        AuditClaim(text="FastAPI released with improved async support."),
    ]
    validated = await auditor._validate_claims(claims, mock_results)
    # Pelo menos uma claim deve ser verificada com alta cobertura
    verified = [c for c in validated if c.status == "verified"]
    assert len(verified) >= 1


@pytest.mark.asyncio
async def test_auditor_detects_gap(mock_llm):
    """Claims sem cobertura devem ser marcadas para rechecagem."""
    auditor = ResearchAuditor(llm_client=mock_llm)
    claims = [AuditClaim(text="Zxqwerty frambula cruxilated.")]
    # Sem resultados → todos gaps
    validated = await auditor._validate_claims(claims, [])
    assert validated[0].needs_recheck is True
    assert validated[0].status == "gap"


@pytest.mark.asyncio
async def test_auditor_full_pipeline(mock_llm, mock_results):
    """Pipeline completo de auditoria retorna AuditReport válido."""
    auditor = ResearchAuditor(llm_client=mock_llm)
    report = await auditor.audit(
        report_text="# Research Report\n\nPython 3.13 supports GIL-free execution.",
        existing_results=mock_results,
        max_iterations=1,
    )
    assert report.total_claims == 3
    assert report.iterations_run >= 1
    assert isinstance(report.enriched_content, str)
    assert "Auditoria de Claims" in report.enriched_content


# ─── HealthMonitor ───────────────────────────────────────────────────────────

from src.monitoring.health_monitor import HealthMonitor, ServiceStatus, ServiceCheck


@pytest.mark.asyncio
async def test_health_monitor_healthy_service():
    """Serviço retornando 200 deve ser marcado como HEALTHY."""
    import httpx

    monitor = HealthMonitor(extra_services=[])
    monitor.services = [
        ServiceCheck(name="test_svc", url="http://test.local/health", timeout_seconds=3.0)
    ]

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        snapshot = await monitor.check_all()

    assert snapshot.services["test_svc"].status == ServiceStatus.HEALTHY
    assert snapshot.services["test_svc"].latency_ms >= 0


@pytest.mark.asyncio
async def test_health_monitor_offline_service():
    """Serviço recusando conexão deve ser marcado como OFFLINE."""
    import httpx

    monitor = HealthMonitor(extra_services=[])
    monitor.services = [
        ServiceCheck(name="offline_svc", url="http://offline.local/health", timeout_seconds=1.0)
    ]

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        snapshot = await monitor.check_all()

    assert snapshot.services["offline_svc"].status == ServiceStatus.OFFLINE


@pytest.mark.asyncio
async def test_health_monitor_fallback_triggered():
    """Fallback deve ser executado quando serviço crítico fica offline."""
    import httpx

    fallback_called = []

    def my_fallback(svc, result):
        fallback_called.append(svc.name)

    monitor = HealthMonitor(extra_services=[])
    monitor.services = [
        ServiceCheck(
            name="critical_svc",
            url="http://critical.local/health",
            timeout_seconds=1.0,
            fallback_action="my_fallback",
            critical=True,
        )
    ]
    monitor.register_fallback("my_fallback", my_fallback)

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        snapshot = await monitor.check_all()

    assert "critical_svc" in fallback_called
    assert snapshot.services["critical_svc"].fallback_triggered is True


@pytest.mark.asyncio
async def test_health_monitor_overall_degraded_on_critical_offline():
    """Status geral deve ser DEGRADED quando serviço crítico está offline."""
    import httpx

    monitor = HealthMonitor(extra_services=[])
    monitor.services = [
        ServiceCheck(
            name="critical",
            url="http://critical.local/h",
            timeout_seconds=1.0,
            critical=True,
        )
    ]

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        snapshot = await monitor.check_all()

    assert snapshot.overall_status == ServiceStatus.DEGRADED
    assert len(snapshot.alerts) >= 1


def test_health_monitor_to_markdown():
    """Snapshot deve gerar Markdown válido."""
    from src.monitoring.health_monitor import HealthSnapshot, ServiceHealthResult

    snapshot = HealthSnapshot(
        timestamp="2026-06-30T00:00:00Z",
        services={
            "firecrawl": ServiceHealthResult(
                name="firecrawl",
                status=ServiceStatus.HEALTHY,
                latency_ms=42.0,
                checked_at="2026-06-30T00:00:00Z",
                detail="HTTP 200",
            )
        },
        overall_status=ServiceStatus.HEALTHY,
        alerts=[],
    )
    md = snapshot.to_markdown()
    assert "firecrawl" in md
    assert "healthy" in md.lower()
    assert "Health Monitor" in md
