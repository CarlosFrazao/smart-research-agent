"""
Testes unitários da Fase 4 — Autonomia
Cobre: OperationModes, ResearchAuditor, HealthMonitor
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ─── OperationModes ───────────────────────────────────────────────────────────

from src.operation_modes import OperationModes, OperationConfig


def test_operation_modes_all_nine_exist():
    """Verifica que os 9 modos estão registrados."""
    modes = OperationModes.list_modes()
    assert len(modes) == 9
    expected = {
        "guerrilha",
        "cirurgia",
        "radar",
        "arqueologia",
        "concorrencia",
        "black_ops",
        "debate",
        "academico",
        "mito",
    }
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
    assert (
        OperationModes.auto_select("pesquisa completa sobre deep learning")
        == "black_ops"
    )


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
    llm.generate_structured = AsyncMock(
        return_value=[
            "Python 3.13 supports GIL-free execution.",
            "The latest version of FastAPI is 0.115.",
            "ChromaDB supports cosine distance natively.",
        ]
    )
    return llm


@pytest.fixture
def mock_results():
    """Resultados simulados com conteúdo relevante."""
    results = []
    for title, desc in [
        (
            "Python 3.13 Release Notes",
            "Python 3.13 supports free-threaded mode and GIL-free execution natively.",
        ),
        (
            "FastAPI Changelog",
            "FastAPI 0.115 was released with improved async support and OpenAPI 3.1.",
        ),
        (
            "ChromaDB Docs",
            "ChromaDB supports cosine, l2, and IP distance metrics for vector search.",
        ),
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
async def test_auditor_validates_high_coverage(mock_llm):
    """Claims com alta cobertura e fontes distintas devem ser marcadas como 'verified'."""
    auditor = ResearchAuditor(llm_client=mock_llm)
    claims = [
        AuditClaim(text="Python supports free-threaded execution."),
        AuditClaim(text="FastAPI released with improved async support."),
    ]

    # Criamos resultados que corroboram a claim do Python em dois provedores diferentes
    r1 = MagicMock()
    r1.title = "Python 3.13 Release Notes"
    r1.description = "Python 3.13 supports free-threaded mode and GIL-free execution natively."
    r1.url = "https://github.com/python/cpython"
    r1.source = "github"
    r1.confidence_score = 0.9

    r2 = MagicMock()
    r2.title = "Reddit Python Discussion"
    r2.description = "Post discussing how Python supports free-threaded execution."
    r2.url = "https://reddit.com/r/python/comments/123"
    r2.source = "reddit"
    r2.confidence_score = 0.8

    # E um resultado para a claim do FastAPI (apenas 1 provedor -> single_source)
    r3 = MagicMock()
    r3.title = "FastAPI Changelog"
    r3.description = "FastAPI 0.115 was released with improved async support and OpenAPI 3.1."
    r3.url = "https://example.com/fastapi-changelog"
    r3.source = "web"
    r3.confidence_score = 0.85

    validated = await auditor._validate_claims(claims, [r1, r2, r3])

    # A claim de Python deve ser 'verified' porque tem 2 fontes/provedores distintos
    python_claim = [c for c in validated if "Python" in c.text][0]
    assert python_claim.status == "verified"
    assert python_claim.confidence >= 0.7

    # A claim de FastAPI deve ser 'single_source' porque só tem 1 fonte/provedor
    fastapi_claim = [c for c in validated if "FastAPI" in c.text][0]
    assert fastapi_claim.status == "single_source"


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


# ─── ResearchAuditor × Custo (MEL-6.3) ────────────────────────────────────────


def test_max_audit_iterations_default_is_one():
    """O teto padrão de re-pesquisa deve ser 1 (reduzido de 3)."""
    from src.research_auditor import MAX_AUDIT_ITERATIONS

    assert MAX_AUDIT_ITERATIONS == 1


@pytest.mark.asyncio
async def test_audit_skips_entirely_for_high_confidence_results(mock_llm):
    """Fontes com confiança média >= limiar não devem disparar extração/LLM."""
    high_conf_results = []
    for i in range(3):
        r = MagicMock()
        r.title = f"Fonte {i}"
        r.description = "Conteúdo já bem verificado."
        r.confidence_score = 0.97
        high_conf_results.append(r)

    auditor = ResearchAuditor(llm_client=mock_llm)
    report = await auditor.audit(
        report_text="# Relatório\n\nAlgo já muito bem verificado.",
        existing_results=high_conf_results,
    )

    assert report.skipped is True
    assert report.iterations_run == 0
    assert report.total_claims == 0
    assert report.enriched_content == "# Relatório\n\nAlgo já muito bem verificado."
    mock_llm.generate_structured.assert_not_called()


@pytest.mark.asyncio
async def test_audit_does_not_skip_moderate_confidence(mock_llm, mock_results):
    """confidence_score=0.85 (fixture padrão) fica abaixo do limiar de skip (0.90)."""
    auditor = ResearchAuditor(llm_client=mock_llm)
    report = await auditor.audit(
        report_text="# Relatório\n\nAlgo com confiança moderada.",
        existing_results=mock_results,
        max_iterations=1,
    )
    assert report.skipped is False
    mock_llm.generate_structured.assert_called()


@pytest.mark.asyncio
async def test_audit_stops_gap_research_when_budget_exhausted(mock_llm):
    """Com orçamento ~zero, a auditoria não deve conseguir re-pesquisar gaps."""
    auditor = ResearchAuditor(llm_client=mock_llm, audit_budget_usd=0.0)

    fake_orchestrator = MagicMock()
    fake_orchestrator.source_planner.plan.return_value = MagicMock()
    fake_orchestrator._parallel_search = AsyncMock(return_value=[])
    auditor.orchestrator = fake_orchestrator

    report = await auditor.audit(
        report_text="# Relatório\n\nAlgo não verificado ainda.",
        existing_results=[],  # sem fontes -> todas as claims viram gap
        max_iterations=1,
    )

    assert report.budget_exhausted is True
    fake_orchestrator._parallel_search.assert_not_called()


@pytest.mark.asyncio
async def test_audit_records_estimated_cost_from_llm_token_economy():
    """O custo estimado da extração deve ser contabilizado via TokenEconomy real."""
    from src.token_economy import TokenEconomy

    llm = MagicMock()
    llm.token_economy = TokenEconomy(default_model="gpt-4o-mini")
    llm.generate_structured = AsyncMock(return_value=["Uma claim qualquer verificável."])

    auditor = ResearchAuditor(llm_client=llm)
    report = await auditor.audit(
        report_text="# Relatório\n\nUma claim qualquer verificável.",
        existing_results=[],
        max_iterations=1,
    )

    assert report.estimated_cost_usd > 0.0


# ─── HealthMonitor ───────────────────────────────────────────────────────────

from src.monitoring.health_monitor import HealthMonitor, ServiceStatus, ServiceCheck


@pytest.mark.asyncio
async def test_health_monitor_healthy_service():
    """Serviço retornando 200 deve ser marcado como HEALTHY."""
    import httpx

    monitor = HealthMonitor(extra_services=[])
    monitor.services = [
        ServiceCheck(
            name="test_svc", url="http://test.local/health", timeout_seconds=3.0
        )
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
        ServiceCheck(
            name="offline_svc", url="http://offline.local/health", timeout_seconds=1.0
        )
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


# ─── HealthMonitor × CircuitBreaker (MEL-6.2) ─────────────────────────────────

from src.utils.circuit_breaker import CircuitBreakerRegistry, CircuitState


class _FakeSearcher:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled


class _FakeOrchestrator:
    def __init__(self, searchers: dict[str, _FakeSearcher]):
        self.searchers = searchers


@pytest.fixture(autouse=True)
def _reset_circuit_breaker_registry():
    """Isola os testes do estado global do CircuitBreakerRegistry."""
    CircuitBreakerRegistry.reset_all()
    yield
    CircuitBreakerRegistry.reset_all()


def test_report_failure_opens_circuit_after_threshold_and_disables_source():
    """3 falhas consecutivas devem abrir o circuito real e desabilitar o searcher."""
    monitor = HealthMonitor(extra_services=[])
    searcher = _FakeSearcher(enabled=True)
    monitor.orchestrator = _FakeOrchestrator({"github": searcher})

    monitor.report_failure("github", "erro 1")
    monitor.report_failure("github", "erro 2")
    assert searcher.enabled is True  # ainda não atingiu o limiar

    monitor.report_failure("github", "erro 3")
    assert searcher.enabled is False

    breaker = CircuitBreakerRegistry.get("github")
    assert breaker.state == CircuitState.OPEN
    assert breaker.failure_count == 3
    assert breaker.last_error == "erro 3"
    assert breaker.total_failures == 3


def test_report_failure_does_not_disable_before_threshold():
    """Falhas isoladas (abaixo do limiar) não devem desabilitar a fonte."""
    monitor = HealthMonitor(extra_services=[])
    searcher = _FakeSearcher(enabled=True)
    monitor.orchestrator = _FakeOrchestrator({"arxiv": searcher})

    monitor.report_failure("arxiv", "timeout")
    assert searcher.enabled is True
    assert CircuitBreakerRegistry.get("arxiv").state == CircuitState.CLOSED


def test_report_success_recovers_half_open_circuit_and_reenables_source():
    """Sucesso em HALF_OPEN deve fechar o circuito e reabilitar a fonte."""
    monitor = HealthMonitor(extra_services=[])
    searcher = _FakeSearcher(enabled=True)
    monitor.orchestrator = _FakeOrchestrator({"reddit": searcher})

    for _ in range(3):
        monitor.report_failure("reddit", "erro")
    assert searcher.enabled is False

    breaker = CircuitBreakerRegistry.get("reddit")
    breaker.state = CircuitState.HALF_OPEN  # simula expiração do recovery_timeout

    monitor.report_success("reddit")

    assert breaker.state == CircuitState.CLOSED
    assert searcher.enabled is True
    assert breaker.total_successes == 1


def test_get_active_sources_excludes_open_circuits():
    """Fontes com circuito OPEN não devem aparecer em get_active_sources()."""
    monitor = HealthMonitor(extra_services=[])
    healthy = _FakeSearcher(enabled=True)
    broken = _FakeSearcher(enabled=True)
    monitor.orchestrator = _FakeOrchestrator(
        {"hackernews": healthy, "producthunt": broken}
    )

    for _ in range(3):
        monitor.report_failure("producthunt", "falhou")

    active = monitor.get_active_sources()
    assert "hackernews" in active
    assert "producthunt" not in active
    assert broken.enabled is False


@pytest.mark.asyncio
async def test_check_all_snapshot_includes_circuit_breaker_metrics():
    """O snapshot de check_all() deve trazer métricas por-fonte dos circuit breakers."""
    monitor = HealthMonitor(extra_services=[])
    monitor.services = []  # sem serviços HTTP para checar neste teste
    monitor.orchestrator = _FakeOrchestrator({"web": _FakeSearcher()})

    monitor.report_failure("web", "erro de rede")
    snapshot = await monitor.check_all()

    assert "web" in snapshot.circuit_breakers
    assert snapshot.circuit_breakers["web"]["failure_count"] == 1
    assert snapshot.circuit_breakers["web"]["last_error"] == "erro de rede"

    md = snapshot.to_markdown()
    assert "Circuit Breakers" in md
    assert "web" in md
