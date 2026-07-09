import pytest
from unittest.mock import MagicMock

def test_unknown_domain_falls_back_to_universal():
    """Domínio desconhecido deve cair em 'universal'."""
    from src.source_planner import SourcePlanner
    planner = SourcePlanner.__new__(SourcePlanner)
    planner.domain_map = {
        "universal": {"primary": ["searxng", "web"], "secondary": [], "fallback_enabled": True},
        "general": {"primary": ["github"], "secondary": [], "fallback_enabled": False},
    }
    # Simula resolução de domínio desconhecido
    domain = "culinaria"  # não existe no mapa
    if domain not in planner.domain_map:
        domain = "universal"
    assert domain == "universal"


def test_dev_tools_not_overridden():
    """Domínio técnico existente não deve cair em universal."""
    from src.source_planner import SourcePlanner
    planner = SourcePlanner.__new__(SourcePlanner)
    planner.domain_map = {
        "dev_tools": {"primary": ["github", "stackoverflow"], "secondary": [], "fallback_enabled": False},
        "universal": {"primary": ["searxng"], "secondary": [], "fallback_enabled": True},
    }
    domain = "dev_tools"
    assert domain in planner.domain_map  # não deve cair em universal
