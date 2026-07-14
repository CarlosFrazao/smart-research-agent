"""Testes de configuração do SearXNG self-hosted (Bloco 4 / E3-T1).

Validam de forma determinística (sem subir Docker) que:
  - o serviço `searxng` está declarado no docker-compose.yml com porta
    configurável (${SEARXNG_PORT:-8080}) e healthcheck;
  - o `docker/searxng/settings.yml` habilita o formato JSON (consumido pelo
    SearXNGSearcher) e os engines-alvo (google, bing, duckduckgo, brave);
  - o `.env.example` documenta SEARXNG_PORT/SEARXNG_ENGINES.

Substituem os passos manuais (`docker-compose up`/`curl`) por asserções
automatizadas sobre os arquivos de configuração, mantendo o bloco verificável
no CI sem depender de um daemon Docker.
"""

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = PROJECT_ROOT / "docker-compose.yml"
SETTINGS_PATH = PROJECT_ROOT / "docker" / "searxng" / "settings.yml"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"


def _load_compose() -> dict:
    """Carrega o docker-compose.yml como dict."""
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_searxng_service_declared() -> None:
    """O serviço `searxng` existe no compose com a imagem oficial."""
    compose = _load_compose()
    assert "searxng" in compose["services"], "serviço searxng ausente no compose"
    service = compose["services"]["searxng"]
    assert "searxng/searxng" in service["image"]
    assert service["container_name"] == "sra-searxng"


def test_searxng_port_is_configurable() -> None:
    """A porta exposta usa a variável ${SEARXNG_PORT:-8080} (não hardcoded)."""
    service = _load_compose()["services"]["searxng"]
    ports = [str(p) for p in service["ports"]]
    assert any("${SEARXNG_PORT:-8080}:8080" in p for p in ports), (
        f"porta não configurável via SEARXNG_PORT: {ports}"
    )


def test_searxng_has_healthcheck() -> None:
    """O serviço possui healthcheck (princípio não-negociável docker-expert)."""
    service = _load_compose()["services"]["searxng"]
    assert "healthcheck" in service
    test_cmd = " ".join(service["healthcheck"]["test"])
    assert "healthz" in test_cmd


def test_searxng_dev_profile() -> None:
    """O serviço pertence aos perfis dev e full."""
    service = _load_compose()["services"]["searxng"]
    assert set(service["profiles"]) >= {"dev", "full"}


def test_settings_enables_json_format() -> None:
    """O settings.yml habilita o formato JSON consumido pelo searcher."""
    settings = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    assert "json" in settings["search"]["formats"], (
        "formato json ausente — SearXNGSearcher exige format=json"
    )


def test_settings_enables_target_engines() -> None:
    """Os engines-alvo (google, bing, duckduckgo, brave) estão habilitados."""
    settings = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    engines = {e["name"]: e for e in settings.get("engines", [])}
    for name in ("google", "bing", "duckduckgo", "brave"):
        assert name in engines, f"engine '{name}' não declarado em settings.yml"
        assert engines[name].get("disabled") is False, (
            f"engine '{name}' deveria estar habilitado (disabled: false)"
        )


def test_settings_uses_default_settings() -> None:
    """Herda os defaults oficiais para não perder engines/config padrão."""
    settings = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    assert settings.get("use_default_settings") is True


def test_env_example_documents_searxng_vars() -> None:
    """O .env.example documenta SEARXNG_PORT e SEARXNG_ENGINES."""
    content = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assert "SEARXNG_PORT" in content
    assert "SEARXNG_ENGINES" in content
