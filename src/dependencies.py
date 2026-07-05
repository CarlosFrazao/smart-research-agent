"""Dependencies — Container de Injeção de Dependências (DI) para o SRA.

Centraliza o registro, resolução e lifecycle de todos os serviços do
Smart Research Agent, eliminando variáveis globais e acoplamento direto
entre módulos.

Funcionalidades:
  - Registro de serviços com nome + factory + lifecycle
  - Singleton vs Transient lifecycle
  - Lazy initialization (só cria quando primeiro requisitado)
  - Resolução de dependências em cadeia (auto-wiring)
  - Override para testes e A/B testing
  - Integração nativa com FastAPI app.state
  - Scopes: application, request, session
  - Validação de dependências circulares
  - Shutdown graceful de serviços stateful
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ── Enums ────────────────────────────────────────────────────────────────────


class Lifecycle(str, Enum):
    """Lifecycle dos serviços registrados."""

    SINGLETON = "singleton"  # Uma instância global, reutilizada sempre
    SCOPED = "scoped"  # Uma instância por scope (request/session)
    TRANSIENT = "transient"  # Nova instância a cada resolve()
    LAZY_SINGLETON = "lazy_singleton"  # Singleton criado no primeiro resolve()


class ScopeType(str, Enum):
    """Tipos de scope suportados."""

    APPLICATION = "application"  # Vida útil = aplicação
    REQUEST = "request"  # Vida útil = uma requisição HTTP
    SESSION = "session"  # Vida útil = sessão do usuário


# ── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass
class ServiceRegistration:
    """Registro de um serviço no container."""

    name: str
    factory: Callable[..., Any]
    lifecycle: Lifecycle
    scope: ScopeType = ScopeType.APPLICATION
    dependencies: List[str] = field(default_factory=list)
    instance: Optional[Any] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_singleton(self) -> bool:
        return self.lifecycle in (Lifecycle.SINGLETON, Lifecycle.LAZY_SINGLETON)


@dataclass
class ContainerConfig:
    """Configuração do container DI."""

    auto_wire: bool = True  # Tenta resolver dependências automaticamente
    validate_on_register: bool = True  # Valida factory no registro
    strict_mode: bool = False  # Se True, erro se dependência não registrada
    log_resolutions: bool = False  # Loga cada resolve()
    enable_scopes: bool = True  # Suporte a scopes request/session
    max_resolution_depth: int = 20  # Previne recursão infinita


# ── Container ─────────────────────────────────────────────────────────────


class Container:
    """Container de Injeção de Dependências para o SRA.

    Substitui variáveis globais (_orchestrator, _deep_researcher) por um
    registro centralizado com lifecycle controlado.

    Args:
        config: Configuração do container.
    """

    def __init__(self, config: Optional[ContainerConfig] = None):
        self.config = config or ContainerConfig()
        self._registry: Dict[str, ServiceRegistration] = {}
        self._overrides: Dict[str, Callable[..., Any]] = {}
        self._scopes: Dict[str, Dict[str, Any]] = {
            ScopeType.APPLICATION.value: {},
        }
        self._scope_lock = threading.Lock()
        self._resolution_stack: List[str] = []
        self._shutdown_callbacks: List[Callable] = []

        # Registro de aliases (ex: "llm" → "llm_client")
        self._aliases: Dict[str, str] = {}

        logger.info("DI Container inicializado")

    # ── Registro ────────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        factory: Union[Type[T], Callable[..., T]],
        lifecycle: Lifecycle = Lifecycle.SINGLETON,
        scope: ScopeType = ScopeType.APPLICATION,
        dependencies: Optional[List[str]] = None,
        alias: Optional[str] = None,
    ) -> "Container":
        """Registra um serviço no container (fluent API)."""
        if name in self._registry:
            logger.warning(
                f"Container: serviço '{name}' já registrado — sobrescrevendo"
            )

        # Auto-detect dependencies da assinatura se não fornecidas
        deps = dependencies or []
        if self.config.auto_wire and not deps:
            deps = self._extract_dependencies(factory)

        reg = ServiceRegistration(
            name=name,
            factory=factory,
            lifecycle=lifecycle,
            scope=scope,
            dependencies=deps,
        )
        self._registry[name] = reg

        if alias:
            self._aliases[alias] = name

        if self.config.log_resolutions:
            logger.debug(f"Container: registrado '{name}' ({lifecycle.value})")

        return self

    def register_instance(
        self,
        name: str,
        instance: Any,
        alias: Optional[str] = None,
    ) -> "Container":
        """Registra uma instância pré-criada (singleton pré-inicializado)."""
        reg = ServiceRegistration(
            name=name,
            factory=lambda: instance,
            lifecycle=Lifecycle.SINGLETON,
            instance=instance,
        )
        self._registry[name] = reg

        if alias:
            self._aliases[alias] = name

        logger.debug(f"Container: instância registrada '{name}'")
        return self

    def register_factory(
        self,
        name: str,
        factory: Callable[..., Any],
        lifecycle: Lifecycle = Lifecycle.TRANSIENT,
        alias: Optional[str] = None,
    ) -> "Container":
        """Registra uma factory customizada."""
        return self.register(name, factory, lifecycle, alias=alias)

    # ── Resolução ─────────────────────────────────────────────────────────────

    def resolve(self, name: str, scope_context: Optional[str] = None) -> Any:
        """Resolve um serviço do container."""
        # Resolve alias
        resolved_name = self._aliases.get(name, name)

        # Verifica override
        if resolved_name in self._overrides:
            return self._overrides[resolved_name]()

        # Verifica registro
        if resolved_name not in self._registry:
            available = list(self._registry.keys()) + list(self._aliases.keys())
            raise DependencyError(
                f"Serviço '{name}' (resolvido: '{resolved_name}') não registrado. "
                f"Disponíveis: {available}"
            )

        reg = self._registry[resolved_name]

        # Previne recursão infinita
        if len(self._resolution_stack) > self.config.max_resolution_depth:
            raise CircularDependencyError(
                f"Profundidade máxima de resolução excedida: {self._resolution_stack}"
            )

        # Previne dependência circular
        if resolved_name in self._resolution_stack:
            raise CircularDependencyError(
                f"Dependência circular detectada: {' -> '.join(self._resolution_stack + [resolved_name])}"
            )

        # Singleton: retorna instância cacheada
        if reg.is_singleton() and reg.instance is not None:
            return reg.instance

        # Scoped: verifica cache do scope e garante que há um contexto de escopo ativo
        if reg.lifecycle == Lifecycle.SCOPED:
            if not scope_context:
                raise ScopeError(
                    f"Serviço scoped '{resolved_name}' só pode ser resolvido "
                    f"dentro de um escopo ativo (request ou session)"
                )
            with self._scope_lock:
                scope_cache = self._scopes.get(scope_context, {})
                if resolved_name in scope_cache:
                    return scope_cache[resolved_name]

        # Cria instância
        self._resolution_stack.append(resolved_name)
        try:
            instance = self._create_instance(reg, scope_context)
        finally:
            self._resolution_stack.pop()

        # Cacheia se singleton
        if reg.is_singleton():
            with reg._lock:
                reg.instance = instance

        # Cacheia se scoped
        if reg.lifecycle == Lifecycle.SCOPED and scope_context:
            with self._scope_lock:
                if scope_context not in self._scopes:
                    self._scopes[scope_context] = {}
                self._scopes[scope_context][resolved_name] = instance

        if self.config.log_resolutions:
            logger.debug(
                f"Container: resolvido '{resolved_name}' ({reg.lifecycle.value})"
            )

        return instance

    def resolve_all(
        self, names: List[str], scope_context: Optional[str] = None
    ) -> Dict[str, Any]:
        """Resolve múltiplos serviços de uma vez."""
        return {name: self.resolve(name, scope_context) for name in names}

    def try_resolve(
        self, name: str, default: Any = None, scope_context: Optional[str] = None
    ) -> Any:
        """Tenta resolver, retorna default se não encontrado."""
        try:
            return self.resolve(name, scope_context)
        except DependencyError:
            return default

    def require(self, name: str, scope_context: Optional[str] = None) -> Any:
        """Alias para resolve() que garante não-None."""
        instance = self.resolve(name, scope_context)
        if instance is None:
            raise DependencyError(f"Serviço '{name}' resolvido como None")
        return instance

    # ── Overrides ─────────────────────────────────────────────────────────────

    def override(
        self,
        name: str,
        factory: Callable[..., Any],
    ) -> "Container":
        """Override de factory para testes ou A/B testing."""
        resolved_name = self._aliases.get(name, name)
        self._overrides[resolved_name] = factory
        if resolved_name in self._registry:
            self._registry[resolved_name].instance = None
        logger.info(f"Container: override registrado para '{resolved_name}'")
        return self

    def remove_override(self, name: str) -> "Container":
        """Remove override de um serviço."""
        resolved_name = self._aliases.get(name, name)
        self._overrides.pop(resolved_name, None)
        logger.debug(f"Container: override removido para '{resolved_name}'")
        return self

    def clear_overrides(self) -> "Container":
        """Remove todos os overrides."""
        self._overrides.clear()
        logger.info("Container: todos os overrides removidos")
        return self

    # ── Scopes ────────────────────────────────────────────────────────────────

    def create_scope(self, scope_type: ScopeType, scope_id: str) -> "Scope":
        """Cria um novo scope para request ou session."""
        return Scope(self, scope_type, scope_id)

    def dispose_scope(self, scope_id: str) -> None:
        """Libera recursos de um scope (chama close() nas instâncias)."""
        with self._scope_lock:
            scope_cache = self._scopes.pop(scope_id, {})

        for name, instance in scope_cache.items():
            self._dispose_instance(instance, name)

        logger.debug(f"Container: scope '{scope_id}' liberado")

    # ── FastAPI Integration ───────────────────────────────────────────────────

    def setup_fastapi(self, app: Any) -> "Container":
        """Integra o container com FastAPI via app.state."""
        try:
            from fastapi import FastAPI
        except ImportError:
            raise ImportError("FastAPI não instalado. Instale com: pip install fastapi")

        app.state.container = self  # type: ignore

        @app.on_event("startup")
        async def startup():
            logger.info("Container: FastAPI startup — inicializando serviços singleton")
            for name, reg in self._registry.items():
                if reg.lifecycle == Lifecycle.SINGLETON and reg.instance is None:
                    try:
                        self.resolve(name)
                    except Exception as e:
                        logger.warning(
                            f"Container: falha ao pré-inicializar '{name}': {e}"
                        )

        @app.on_event("shutdown")
        async def shutdown():
            logger.info("Container: FastAPI shutdown — liberando recursos")
            await self.shutdown()

        logger.info("Container: integrado com FastAPI app.state")
        return self

    def get_fastapi_dependency(self, name: str):
        """Retorna função para uso com FastAPI Depends()."""

        def _dependency(request: Any) -> Any:
            scope_id = None
            if hasattr(request, "state") and hasattr(request.state, "scope_id"):
                scope_id = request.state.scope_id
            return self.resolve(name, scope_context=scope_id)

        return _dependency

    # ── Lifecycle Management ──────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Libera todos os recursos: singletons, scopes, callbacks."""
        for callback in self._shutdown_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback()
                else:
                    callback()
            except Exception as e:
                logger.warning(f"Container: erro em shutdown callback: {e}")

        for name, reg in self._registry.items():
            if reg.instance is not None:
                self._dispose_instance(reg.instance, name)
                reg.instance = None

        scope_ids = list(self._scopes.keys())
        for scope_id in scope_ids:
            if scope_id != ScopeType.APPLICATION.value:
                self.dispose_scope(scope_id)

        self._shutdown_callbacks.clear()
        logger.info("Container: shutdown completo")

    def on_shutdown(self, callback: Callable) -> "Container":
        """Registra callback para executar no shutdown."""
        self._shutdown_callbacks.append(callback)
        return self

    # ── Introspection ─────────────────────────────────────────────────────────

    def is_registered(self, name: str) -> bool:
        """Verifica se serviço está registrado."""
        resolved = self._aliases.get(name, name)
        return resolved in self._registry

    def get_registration(self, name: str) -> Optional[ServiceRegistration]:
        """Retorna registro de um serviço."""
        resolved = self._aliases.get(name, name)
        return self._registry.get(resolved)

    def list_services(self) -> List[Dict[str, Any]]:
        """Lista todos os serviços registrados."""
        result = []
        for name, reg in self._registry.items():
            result.append(
                {
                    "name": name,
                    "lifecycle": reg.lifecycle.value,
                    "scope": reg.scope.value,
                    "dependencies": reg.dependencies,
                    "has_instance": reg.instance is not None,
                    "has_override": name in self._overrides,
                }
            )
        return result

    def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do container."""
        return {
            "registered_services": len(self._registry),
            "active_singletons": sum(
                1 for r in self._registry.values() if r.instance is not None
            ),
            "active_scopes": len(self._scopes) - 1,
            "overrides": len(self._overrides),
            "aliases": len(self._aliases),
            "resolution_stack": list(self._resolution_stack),
        }

    async def close(self) -> None:
        """Encerra todas as instâncias ativas que possuem método close()."""
        instances = []
        for reg in self._registry.values():
            if reg.instance is not None:
                instances.append(reg.instance)
        for inst in instances:
            if hasattr(inst, "close"):
                try:
                    close_fn = getattr(inst, "close")
                    if inspect.iscoroutinefunction(close_fn):
                        await close_fn()
                    else:
                        close_fn()
                except Exception as e:
                    logger.error(f"Erro ao fechar dependência {inst}: {e}")

    def __getattr__(self, name: str) -> Any:
        """Permite acessar serviços registrados diretamente como atributos do container."""
        if self.is_registered(name) or name in self._aliases:
            return self.resolve(name)
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    # ── Helpers internos ────────────────────────────────────────────────────

    def _create_instance(
        self,
        reg: ServiceRegistration,
        scope_context: Optional[str],
    ) -> Any:
        """Cria instância resolvendo dependências automaticamente."""
        if inspect.isclass(reg.factory):
            return self._create_class_instance(reg, scope_context)
        return self._create_callable_instance(reg, scope_context)

    def _create_class_instance(
        self,
        reg: ServiceRegistration,
        scope_context: Optional[str],
    ) -> Any:
        """Cria instância de classe com auto-wire do construtor."""
        try:
            sig = inspect.signature(reg.factory.__init__)
        except (ValueError, TypeError):
            return reg.factory()

        kwargs = {}
        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls", "args", "kwargs"):
                continue

            if self.is_registered(param_name):
                kwargs[param_name] = self.resolve(param_name, scope_context)
            elif param.default is not inspect.Parameter.empty:
                kwargs[param_name] = param.default
            elif self.config.strict_mode:
                raise DependencyError(
                    f"Dependência '{param_name}' de '{reg.name}' não registrada"
                )

        return reg.factory(**kwargs)

    def _create_callable_instance(
        self,
        reg: ServiceRegistration,
        scope_context: Optional[str],
    ) -> Any:
        """Cria instância via callable/factory function."""
        sig = inspect.signature(reg.factory)
        kwargs = {}

        for param_name, param in sig.parameters.items():
            if param_name in ("args", "kwargs"):
                continue

            if self.is_registered(param_name):
                kwargs[param_name] = self.resolve(param_name, scope_context)
            elif param.default is not inspect.Parameter.empty:
                kwargs[param_name] = param.default
            elif self.config.strict_mode:
                raise DependencyError(
                    f"Dependência '{param_name}' de factory '{reg.name}' não registrada"
                )

        return reg.factory(**kwargs)

    def _extract_dependencies(self, factory: Callable) -> List[str]:
        """Extrai nomes de dependências da assinatura da factory."""
        try:
            sig = inspect.signature(factory)
        except ValueError:
            return []

        deps = []
        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls", "args", "kwargs"):
                continue
            if param.default is inspect.Parameter.empty:
                deps.append(param_name)
        return deps

    def _dispose_instance(self, instance: Any, name: str) -> None:
        """Chama close() ou aclose() na instância se disponível."""
        try:
            if hasattr(instance, "close") and callable(instance.close):
                if asyncio.iscoroutinefunction(instance.close):
                    asyncio.create_task(instance.close())
                else:
                    instance.close()
            elif hasattr(instance, "aclose") and callable(instance.aclose):
                asyncio.create_task(instance.aclose())
        except Exception as e:
            logger.warning(f"Container: erro ao dispose '{name}': {e}")


# ── Scope ───────────────────────────────────────────────────────────────────


class Scope:
    """Context manager para scopes de request/session."""

    def __init__(self, container: Container, scope_type: ScopeType, scope_id: str):
        self.container = container
        self.scope_type = scope_type
        self.scope_id = scope_id

    def __enter__(self) -> "Scope":
        with self.container._scope_lock:
            self.container._scopes[self.scope_id] = {}
        logger.debug(f"Scope '{self.scope_id}' ({self.scope_type.value}) iniciado")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.container.dispose_scope(self.scope_id)
        logger.debug(f"Scope '{self.scope_id}' ({self.scope_type.value}) finalizado")

    def resolve(self, name: str) -> Any:
        """Resolve serviço dentro deste scope."""
        return self.container.resolve(name, scope_context=self.scope_id)


# ── FastAPI Helpers ──────────────────────────────────────────────────────────


def fastapi_dependency(name: str):
    """Factory de dependências para FastAPI Depends()."""

    def _resolver(request: Any) -> Any:
        container = getattr(request.app.state, "container", None)
        if container is None:
            raise DependencyError(
                "Container não encontrado em app.state. "
                "Chame container.setup_fastapi(app) no startup."
            )
        scope_id = getattr(request.state, "scope_id", None)
        return container.resolve(name, scope_context=scope_id)

    _resolver.__name__ = f"resolve_{name}"
    return _resolver


# ── Exceções ─────────────────────────────────────────────────────────────────


class DependencyError(Exception):
    """Erro na resolução de dependências."""

    pass


class CircularDependencyError(DependencyError):
    """Dependência circular detectada."""

    pass


class ScopeError(Exception):
    """Erro relacionado a scopes."""

    pass


# Alias de retrocompatibilidade para outros módulos
DependencyContainer = Container
