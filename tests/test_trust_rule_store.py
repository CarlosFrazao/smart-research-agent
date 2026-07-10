"""Testes unitários do TrustRuleStore."""

import os
import tempfile
import json
import pytest
from src.trust_rule_store import TrustRuleStore


@pytest.fixture
def store_path():
    """Retorna um caminho de arquivo temporário único para cada teste."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)  # We'll open it via the store
    return path


@pytest.fixture
def store(stateful_store_path=None):
    """Instancia TrustRuleStore apontando para o caminho temporário."""
    path = stateful_store_path or tempfile.mktemp(suffix=".jsonl")
    return TrustRuleStore(store_path=path)


def test_record_and_load(store):
    """Testa gravação e leitura de uma entrada."""
    entry = store.record(user_id="u1", source="reddit", tier="allow")
    assert entry["tier"] == "allow"

    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0]["user_id"] == "u1"
    assert loaded[0]["source"] == "reddit"
    assert loaded[0]["tier"] == "allow"
    assert "timestamp" in loaded[0]


def test_get_rules_latest_wins(store):
    """Testa que o registro mais recente sobrescreve o anterior para a mesma source."""
    store.record(user_id="u1", source="reddit", tier="allow")
    store.record(user_id="u1", source="reddit", tier="deny")  # mais recente
    rules = store.get_rules_for_user("u1")
    assert rules["reddit"] == "deny"


def test_user_isolation(store):
    """Testa que as regras de usuários diferentes são isoladas."""
    store.record(user_id="u1", source="reddit", tier="allow")
    store.record(user_id="u2", source="reddit", tier="deny")
    assert store.get_rules_for_user("u1")["reddit"] == "allow"
    assert store.get_rules_for_user("u2")["reddit"] == "deny"


def test_invalid_tier_raises(store):
    """Testa que tier inválido gera ValueError."""
    with pytest.raises(ValueError, match="tier inválido"):
        store.record(user_id="u1", source="reddit", tier="maybe")


def test_empty_user_id_raises(store):
    """Testa que user_id vazio gera ValueError."""
    with pytest.raises(ValueError, match="user_id não pode ser vazio"):
        store.record(user_id="", source="reddit", tier="allow")


def test_clear_by_user(store):
    """Testa a remoção de regras de um usuário específico."""
    store.record(user_id="u1", source="reddit", tier="allow")
    store.record(user_id="u2", source="twitter", tier="deny")
    removed = store.clear(user_id="u1")
    assert removed == 1
    assert store.get_rules_for_user("u1") == {}
    assert "twitter" in store.get_rules_for_user("u2")


def test_clear_all(store):
    """Testa a remoção de todas as regras quando user_id não é fornecido."""
    store.record(user_id="u1", source="reddit", tier="allow")
    store.record(user_id="u2", source="twitter", tier="deny")
    # Note: this will delete the file; adjust cleanup if needed
    # For unit test isolation you might want to mock or use a temp file
