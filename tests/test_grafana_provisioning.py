"""Testes determinísticos de provisioning do Bloco 13 (E8-T1 — Grafana + Prometheus).

Estes testes NÃO exigem Docker: validam a integridade dos arquivos de
composição e provisioning para garantir que o stack sobe e o Grafana importa
o dashboard de qualidade automaticamente.

Cobertura:
- docker-compose.yml declara os services prometheus/grafana no perfil "monitoring"
  e expõe a porta 8001 (métricas) do SRA.
- docker/prometheus/prometheus.yml faz scrape do alvo correto (smart-research-agent:8001).
- provisioning do Grafana (datasource + dashboard) é YAML/JSON válido e aponta
  para o datasource provisionado.
- o dashboard referencia apenas métricas realmente emitidas por src/observability/metrics.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
COMPOSE_PATH = PROJECT_ROOT / "docker-compose.yml"
PROMETHEUS_CONFIG = PROJECT_ROOT / "docker/prometheus/prometheus.yml"
DATASOURCE_CONFIG = (
    PROJECT_ROOT / "docker/grafana/provisioning/datasources/prometheus.yml"
)
DASHBOARD_CONFIG = (
    PROJECT_ROOT / "docker/grafana/provisioning/dashboards/dashboards.yml"
)
DASHBOARD_JSON = PROJECT_ROOT / "docker/grafana/provisioning/dashboards/sra_quality.json"

# Métricas efetivamente registradas em src/observability/metrics.py
KNOWN_METRICS = {
    "sra_search_requests_total",
    "sra_search_duration_seconds",
    "sra_llm_tokens_total",
    "sra_circuit_breaker_state",
    "sra_cache_hits_total",
    "sra_ragas_faithfulness_score",
    "sra_ragas_relevancy_score",
    "sra_ragas_traceability_score",
}


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "service",
    ["prometheus", "grafana", "smart-research-agent"],
)
def test_compose_declares_services(service: str) -> None:
    compose = _load_yaml(COMPOSE_PATH)
    assert service in compose.get("services", {}), (
        f"service '{service}' ausente no docker-compose.yml"
    )


def test_monitoring_profile_present() -> None:
    compose = _load_yaml(COMPOSE_PATH)
    services = compose["services"]
    assert "monitoring" in services["prometheus"].get("profiles", [])
    assert "monitoring" in services["grafana"].get("profiles", [])
    # Acessíveis também via perfil "full"
    assert "full" in services["prometheus"].get("profiles", [])
    assert "full" in services["grafana"].get("profiles", [])


def test_sra_exposes_metrics_port_8001() -> None:
    compose = _load_yaml(COMPOSE_PATH)
    ports = compose["services"]["smart-research-agent"].get("ports", [])
    assert any(str(p).startswith("8001:8001") for p in ports), (
        "porta 8001 (métricas Prometheus) não exposta pelo SRA"
    )


def test_prometheus_scrapes_correct_target() -> None:
    config = _load_yaml(PROMETHEUS_CONFIG)
    jobs = config["scrape_configs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_name"] == "sra-metrics"
    assert job["metrics_path"] == "/metrics"
    targets = job["static_configs"][0]["targets"]
    assert "smart-research-agent:8001" in targets, (
        "Prometheus deve raspar smart-research-agent:8001 (container do SRA)"
    )


def test_grafana_datasource_provisioned() -> None:
    ds = _load_yaml(DATASOURCE_CONFIG)
    assert ds["apiVersion"] == 1
    datasources = ds["datasources"]
    assert len(datasources) == 1
    src = datasources[0]
    assert src["type"] == "prometheus"
    assert src["url"] == "http://prometheus:9090", (
        "datasource deve apontar para o serviço prometheus do compose"
    )
    # uid estável referenciado pelo dashboard
    assert src["uid"] == "prometheus-sra"
    assert src.get("isDefault") is True


def test_grafana_dashboard_provider_config() -> None:
    provider = _load_yaml(DASHBOARD_CONFIG)
    assert provider["apiVersion"] == 1
    providers = provider["providers"]
    assert len(providers) == 1
    assert providers[0]["type"] == "file"
    assert providers[0]["options"]["path"].endswith("dashboards")


def test_dashboard_json_valid_and_uses_stable_uid() -> None:
    dash = _load_json(DASHBOARD_JSON)
    assert dash["uid"] == "sra-quality"
    assert dash["title"]
    assert len(dash["panels"]) >= 4  # ao menos os 4 painéis exigidos pelo task
    # Todo painel deve referenciar o datasource provisionado por uid estável
    for panel in dash["panels"]:
        ds = panel.get("datasource", {})
        assert ds.get("uid") == "prometheus-sra", (
            f"painel '{panel.get('title')}' não usa o uid estável do datasource"
        )


def test_dashboard_queries_only_known_metrics() -> None:
    dash = _load_json(DASHBOARD_JSON)
    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            for metric in KNOWN_METRICS:
                if metric in expr:
                    break
            else:
                # se a query referencia alguma métrica sra_, ela deve ser conhecida
                referenced = [m for m in KNOWN_METRICS if m.split("_", 1)[0] in expr]
                assert not any(
                    tok.startswith("sra_") for tok in expr.replace("(", " ").split()
                ), f"painel '{panel.get('title')}' referencia métrica sra_ desconhecida: {expr}"


def test_required_panels_present() -> None:
    dash = _load_json(DASHBOARD_JSON)
    titles = {p["title"] for p in dash["panels"]}
    required = {
        "RAGAS Faithfulness ao longo do tempo",
        "RAGAS Relevancy ao longo do tempo",
        "Cache Hit Rate",
        "Latência P95 por modo (segundos)",
        "Top fontes por erro (últimas 24h)",
    }
    assert required.issubset(titles), f"painéis faltando: {required - titles}"
