import pytest
import os
import uuid
import json
import shutil
from unittest.mock import AsyncMock, MagicMock, patch
from src.scheduler import ResearchScheduler, ScheduledJob


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def jobs_file(tmp_path):
    return str(tmp_path / "test_jobs.json")


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch.research = AsyncMock(return_value="# Relatório de Teste\n\n## 2. Fontes\n\n---\n")
    return orch


@pytest.fixture
def scheduler(mock_orchestrator, jobs_file):
    return ResearchScheduler(orchestrator=mock_orchestrator, jobs_file=jobs_file)


# ── Testes de Criação e Listagem de Jobs ──────────────────────────────────────

def test_schedule_research_creates_job(scheduler):
    """Testa criação de job com ID único e persistência."""
    job_id = scheduler.schedule_research(
        query="AI trends 2026",
        cron_expr="0 9 * * 1",
        output_dir="reports/test_scheduled",
    )
    assert isinstance(job_id, str)
    assert len(job_id) == 36  # UUID padrão

    jobs = scheduler.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == job_id
    assert jobs[0]["query"] == "AI trends 2026"
    assert jobs[0]["cron"] == "0 9 * * 1"
    assert jobs[0]["alert_on_changes"] is True


def test_schedule_research_stores_webhook(scheduler):
    """Verifica que webhook_url é armazenado no job."""
    webhook = "https://hooks.slack.com/test"
    job_id = scheduler.schedule_research(
        query="Python frameworks",
        cron_expr="0 8 * * *",
        output_dir="reports/test",
        webhook_url=webhook,
    )
    jobs = scheduler.list_jobs()
    assert jobs[0]["webhook_url"] == webhook


def test_list_jobs_empty(scheduler):
    """Verifica listagem quando não há jobs registrados."""
    assert scheduler.list_jobs() == []


def test_list_jobs_multiple(scheduler):
    """Verifica listagem com múltiplos jobs."""
    scheduler.schedule_research("query 1", "0 9 * * 1", "reports/")
    scheduler.schedule_research("query 2", "0 10 * * 2", "reports/")
    jobs = scheduler.list_jobs()
    assert len(jobs) == 2
    queries = {j["query"] for j in jobs}
    assert queries == {"query 1", "query 2"}


# ── Testes de Persistência ────────────────────────────────────────────────────

def test_jobs_are_persisted_to_json(scheduler, jobs_file):
    """Verifica que jobs são escritos no arquivo JSON após schedule_research."""
    scheduler.schedule_research("open source crm", "0 9 * * 1", "reports/")
    assert os.path.exists(jobs_file)
    with open(jobs_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 1
    assert list(data.values())[0]["query"] == "open source crm"


def test_jobs_are_loaded_from_json(mock_orchestrator, jobs_file):
    """Verifica que jobs são carregados corretamente de um arquivo JSON pré-existente."""
    # Cria um scheduler e adiciona um job
    s1 = ResearchScheduler(orchestrator=mock_orchestrator, jobs_file=jobs_file)
    job_id = s1.schedule_research("load test query", "0 6 * * *", "reports/")

    # Cria outro scheduler com o mesmo arquivo — deve carregar os jobs existentes
    s2 = ResearchScheduler(orchestrator=mock_orchestrator, jobs_file=jobs_file)
    jobs = s2.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == job_id
    assert jobs[0]["query"] == "load test query"


# ── Testes de Cancelamento ────────────────────────────────────────────────────

def test_cancel_job_removes_it(scheduler):
    """Testa que cancel_job remove o job e retorna True."""
    job_id = scheduler.schedule_research("to be removed", "0 9 * * 1", "reports/")
    result = scheduler.cancel_job(job_id)
    assert result is True
    assert scheduler.list_jobs() == []


def test_cancel_nonexistent_job_returns_false(scheduler):
    """Testa que cancel_job retorna False para ID inexistente."""
    result = scheduler.cancel_job("nonexistent-uuid")
    assert result is False


# ── Testes de Comparação de Relatórios ───────────────────────────────────────

def test_compare_with_previous_detects_new_entities(scheduler):
    """Detecta novas entidades (nomes próprios) entre relatórios."""
    old = "# Report\n\nPython é uma linguagem popular."
    new = "# Report\n\nPython é uma linguagem popular.\n\nRust é um novo favorito de Microsoft."
    changes = scheduler.compare_with_previous(new, old)
    assert any("Rust" in c or "Microsoft" in c for c in changes)


def test_compare_with_previous_detects_new_headings(scheduler):
    """Detecta novos tópicos cobertos (## headings)."""
    old = "# Report\n\n## Resumo\n\nConteúdo antigo."
    new = "# Report\n\n## Resumo\n\nConteúdo antigo.\n\n## Tendências Futuras\n\nNovo conteúdo."
    changes = scheduler.compare_with_previous(new, old)
    assert any("Tendências Futuras" in c for c in changes)


def test_compare_with_previous_detects_score_change(scheduler):
    """Detecta mudança no Research Score."""
    old = "# Report\n\nResearch Score: **B**\n\n---"
    new = "# Report\n\nResearch Score: **A+**\n\n---"
    changes = scheduler.compare_with_previous(new, old)
    assert any("B" in c and "A+" in c for c in changes)


def test_compare_identical_reports_no_changes(scheduler):
    """Retorna lista vazia quando relatórios são idênticos."""
    report = "# Report\n\nConteúdo idêntico.\n\n---"
    changes = scheduler.compare_with_previous(report, report)
    assert changes == []


# ── Teste de Execução de Job ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_scheduled_research_calls_orchestrator(scheduler, tmp_path):
    """Testa que run_scheduled_research chama orchestrator.research e salva o arquivo."""
    job_id = scheduler.schedule_research(
        query="open source CRM 2026",
        cron_expr="0 9 * * 1",
        output_dir=str(tmp_path / "scheduled"),
    )

    report = await scheduler.run_scheduled_research(job_id)
    assert "Relatório de Teste" in report

    scheduler.orchestrator.research.assert_called_once_with("open source CRM 2026")

    # Verifica que o arquivo foi salvo
    files = list((tmp_path / "scheduled").glob("*.md"))
    assert len(files) == 1

    # Verifica que last_run e last_report_path foram atualizados
    jobs = scheduler.list_jobs()
    assert jobs[0]["last_run"] is not None
    assert jobs[0]["last_report_path"] is not None


@pytest.mark.asyncio
async def test_run_nonexistent_job_raises(scheduler):
    """Verifica que executar um job inexistente levanta ValueError."""
    with pytest.raises(ValueError, match="não encontrado"):
        await scheduler.run_scheduled_research("fake-id")


# ── Teste de Alerta ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_alert_without_webhook_only_logs(scheduler):
    """Quando não há webhook, send_alert apenas loga e não lança exceção."""
    # Não deve lançar nenhuma exceção
    await scheduler.send_alert(["🆕 Nova entidade: Rust"], webhook_url=None)
