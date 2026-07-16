"""Testes TDD para FEAT-001: resolução defensiva de headers no GenericAPISearcher.

Cobertura (PRD 4.1.2/4.1.3):
- Happy: env var presente -> header montado normalmente.
- Sad: env var ausente -> header OMITIDO (não "Bearer " vazio) + 1 warning unico.
- Edge: env var vazia ("") -> tratada como ausente (omitido).
- Edge: multiplos headers -> cada um resolvido independentemente.
"""

from __future__ import annotations

import logging
import os

import pytest

from src.search.generic_api_searcher import GenericAPISearcher


@pytest.fixture
def searcher() -> GenericAPISearcher:
    """Instancia mínima do GenericAPISearcher (sem catálogo real)."""
    return GenericAPISearcher(source_id="core_ac_uk")


def test_header_env_present_is_mounted(searcher: GenericAPISearcher) -> None:
    """Happy: CORE_API_KEY presente -> header 'Bearer <key>' montado."""
    os.environ["CORE_API_KEY"] = "abc123"  # pragma: allowlist secret
    try:
        raw = {"Authorization": "Bearer {CORE_API_KEY}"}
        resolved = searcher._build_headers(raw)
    finally:
        del os.environ["CORE_API_KEY"]
    assert resolved["Authorization"] == "Bearer abc123"


def test_header_env_absent_is_omitted_with_single_warning(
    searcher: GenericAPISearcher, caplog: pytest.LogCaptureFixture
) -> None:
    """Sad: CORE_API_KEY ausente -> header omitido + exatamente 1 warning."""
    os.environ.pop("CORE_API_KEY", None)
    raw = {"Authorization": "Bearer {CORE_API_KEY}"}

    with caplog.at_level(logging.WARNING, logger="search.generic_api"):
        resolved = searcher._build_headers(raw)

    assert "Authorization" not in resolved
    assert resolved == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    # Nao deve vazar o valor da env var no log.
    assert "Bearer" not in warnings[0].getMessage()


def test_header_env_empty_string_is_omitted(
    searcher: GenericAPISearcher, caplog: pytest.LogCaptureFixture
) -> None:
    """Edge: CORE_API_KEY vazio -> tratado como ausente (omitido)."""
    os.environ["CORE_API_KEY"] = ""
    try:
        raw = {"Authorization": "Bearer {CORE_API_KEY}"}
        with caplog.at_level(logging.WARNING, logger="search.generic_api"):
            resolved = searcher._build_headers(raw)
    finally:
        del os.environ["CORE_API_KEY"]

    assert "Authorization" not in resolved
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_multiple_headers_resolved_independently(
    searcher: GenericAPISearcher, caplog: pytest.LogCaptureFixture
) -> None:
    """Edge: headers misturam presente/ausente -> cada um resolvido isolado."""
    os.environ.pop("MISSING_KEY", None)
    os.environ["PRESENT_KEY"] = "tok"
    try:
        raw = {
            "Authorization": "Bearer {MISSING_KEY}",
            "X-Token": "{PRESENT_KEY}",
        }
        with caplog.at_level(logging.WARNING, logger="search.generic_api"):
            resolved = searcher._build_headers(raw)
    finally:
        del os.environ["PRESENT_KEY"]

    assert "Authorization" not in resolved
    assert resolved.get("X-Token") == "tok"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
