from __future__ import annotations
import pytest
from src.dependencies import (
    Container,
    Lifecycle,
    ScopeType,
    DependencyError,
    CircularDependencyError,
    ScopeError,
)

class MockLLM:
    def __init__(self):
        self.val = "llm"

class MockCache:
    def __init__(self, mock_llm: MockLLM):
        self.llm = mock_llm
        self.val = "cache"

class MockOrchestrator:
    def __init__(self, mock_llm: MockLLM, mock_cache: MockCache):
        self.llm = mock_llm
        self.cache = mock_cache

class CircularA:
    def __init__(self, circular_b: CircularB):
        self.b = circular_b

class CircularB:
    def __init__(self, circular_a: CircularA):
        self.a = circular_a

def test_container_di_auto_wiring():
    container = Container()

    # Registra
    container.register("mock_llm", MockLLM, Lifecycle.LAZY_SINGLETON)
    container.register("mock_cache", MockCache, Lifecycle.SINGLETON)
    container.register("mock_orchestrator", MockOrchestrator, Lifecycle.TRANSIENT)

    # Resolve em cascata
    orch = container.resolve("mock_orchestrator")
    assert isinstance(orch, MockOrchestrator)
    assert isinstance(orch.llm, MockLLM)
    assert isinstance(orch.cache, MockCache)
    assert orch.cache.llm is orch.llm  # Singleton reutilizado

def test_container_overrides():
    container = Container()
    container.register("mock_llm", MockLLM, Lifecycle.SINGLETON)

    # Resolve normal
    llm1 = container.resolve("mock_llm")
    assert llm1.val == "llm"

    # Override
    class MockLLMOverride:
        def __init__(self):
            self.val = "override"

    container.override("mock_llm", lambda: MockLLMOverride())

    llm2 = container.resolve("mock_llm")
    assert llm2.val == "override"

    # Remove override
    container.remove_override("mock_llm")
    llm3 = container.resolve("mock_llm")
    assert llm3.val == "llm"

def test_container_circular_dependencies():
    container = Container()
    container.register("circular_a", CircularA, Lifecycle.TRANSIENT)
    container.register("circular_b", CircularB, Lifecycle.TRANSIENT)

    with pytest.raises(CircularDependencyError):
        container.resolve("circular_a")

def test_container_scopes():
    container = Container()
    container.register("mock_llm", MockLLM, Lifecycle.SCOPED, scope=ScopeType.REQUEST)

    # Fora de escopo lança erro se tentarmos sem scope_id
    with pytest.raises(ScopeError):
        container.resolve("mock_llm")

    # Com escopo funciona
    with container.create_scope(ScopeType.REQUEST, "req_123") as scope:
        llm1 = scope.resolve("mock_llm")
        llm2 = scope.resolve("mock_llm")
        assert llm1 is llm2  # Reutiliza no mesmo escopo

    # Outro escopo cria nova instância
    with container.create_scope(ScopeType.REQUEST, "req_456") as scope2:
        llm3 = scope2.resolve("mock_llm")
        assert llm3 is not llm1
