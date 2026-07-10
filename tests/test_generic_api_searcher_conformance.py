"""Teste de conformidade do catálogo generic_sources.yaml (Plano Parte 3 — Fase 3).

Para toda entrada habilitada em ``config/generic_sources.yaml``, confirma que:
  1. ``result_path`` (JMESPath) resolve para lista não-vazia no payload de fixture
     (quando ``result_path`` é definido);
  2. Todo campo em ``title_field`` / ``snippet_field`` resolve sem erro no
     primeiro item (ou na raiz, quando ``result_path`` é null).

Equivalente ao "teste de reachability" da Parte 2 — garante que nenhum YAML mal
escrito falhe silenciosamente nem aponte para caminhos inexistentes.

As fixtures vivem em ``tests/fixtures/generic_sources/{id}.json`` e foram
capturadas por chamadas reais às APIs (ver TAREFA 3.3 da missão).
"""

import json

import pytest
import yaml
from pathlib import Path

from src.search.generic_api_searcher import (
    _resolve_field,
    list_generic_source_ids,
)

CONFIG_PATH = Path("config/generic_sources.yaml")
FIXTURES_DIR = Path("tests/fixtures/generic_sources")


def _load_enabled_sources() -> list[dict]:
    """Retorna as definições de fonte habilitadas no catálogo YAML."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        all_sources = yaml.safe_load(f).get("sources", [])
    return [s for s in all_sources if s.get("enabled", True)]


@pytest.mark.parametrize(
    "source_cfg", _load_enabled_sources(), ids=lambda c: c["id"]
)
def test_source_conformance(source_cfg):
    source_id = source_cfg["id"]
    fixture_path = FIXTURES_DIR / f"{source_id}.json"
    assert fixture_path.exists(), (
        f"Fonte '{source_id}' não tem fixture em {fixture_path}. "
        f"Crie uma com uma chamada real à API (TAREFA 3.3)."
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    result_path = source_cfg.get("result_path")
    if result_path:
        items = _resolve_list(payload, result_path)
        assert items and isinstance(items, list), (
            f"result_path '{result_path}' da fonte '{source_id}' "
            f"não resolveu para lista não-vazia no fixture."
        )
        first = items[0]
    else:
        # result_path null: o searcher itera a própria resposta (lista raiz)
        # ou usa o dict raiz diretamente. Para validar o mapping, inspeciona
        # o primeiro item quando a resposta é uma lista.
        first = payload[0] if isinstance(payload, list) and payload else payload

    mapping = source_cfg
    for field_name in ("title_field", "snippet_field"):
        path = mapping.get(field_name)
        if path and not str(path).startswith("static:"):
            value = _resolve_field(first, path)
            assert value not in (None, ""), (
                f"Campo '{field_name}' (path='{path}') da fonte '{source_id}' "
                f"resolveu para vazio no fixture — mapping provavelmente errado."
            )


def _resolve_list(data, result_path: str) -> list:
    """Resolve result_path via JMESPath (mesma lógica do searcher)."""
    try:
        import jmespath

        extracted = jmespath.search(result_path, data)
    except Exception:
        return []
    return extracted if isinstance(extracted, list) else []


def test_all_eight_sources_present():
    """A Fase 3 exige 8 fontes no catálogo (open_meteo desabilitado por padrão)."""
    ids = set(list_generic_source_ids())
    expected = {
        "wikipedia",
        "open_library",
        "npm_registry",
        "dictionary",
        "musicbrainz",
        "domain_whois",
        "core_ac_uk",
        "doaj",
        "osm_nominatim",
        "openalex",
    }
    assert expected.issubset(ids), f"Fontes faltando: {expected - ids}"
