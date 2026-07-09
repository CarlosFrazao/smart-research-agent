"""Testes para o registro automático de searchers."""

import pytest
import os
from src.search.registry import register_searcher, get_registry, list_registered

def test_register_searcher_decorator():
    """Testa que o decorator @register_searcher registra corretamente."""
    # Registrando um searcher de teste
    @register_searcher("test_dummy_xyz", trusted=True)
    class DummySearcher:
        pass

    registry = get_registry()
    assert "test_dummy_xyz" in registry
    assert registry["test_dummy_xyz"]["cls"] is DummySearcher
    assert registry["test_dummy_xyz"]["trusted"] is True


def test_list_registered_includes_new():
    """Testa que list_registered inclui novos searchers registrados."""
    @register_searcher("test_dummy_list_xyz")
    class AnotherDummy:
        pass

    names = list_registered()
    assert "test_dummy_list_xyz" in names


def test_requires_key_metadata():
    """Testa que requires_key é armazenado corretamente no registro."""
    @register_searcher("test_keyed_xyz", requires_key="FAKE_API_KEY_ENV")
    class KeyedSearcher:
        pass

    registry = get_registry()
    assert registry["test_keyed_xyz"]["requires_key"] == "FAKE_API_KEY_ENV"


def test_enabled_env_defaults_to_none():
    """Testa que enabled_env é None por padrão."""
    @register_searcher("test_no_env_xyz")
    class NoEnvSearcher:
        pass

    registry = get_registry()
    assert registry["test_no_env_xyz"]["enabled_env"] is None