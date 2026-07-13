"""Tracing distribuído com OpenTelemetry para o Smart Research Agent.

Fornece:
  - Correlation IDs para rastreamento de requests end-to-end
  - Spans para cada stage do pipeline de pesquisa
  - Spans para chamadas LLM e searchers
  - Export para Jaeger/Zipkin (OTLP)
  - Integração transparente com structlog (injeção de trace_id/span_id nos logs)

Design:
  - TracerProvider singleton lazy-loaded — inicializa apenas se opentelemetry estiver instalado.
  - Context propagation via contextvars (thread-safe e async-safe).
  - Decorator `@trace_span(name, kind)` para instrumentação não-invasiva.
  - Context manager `trace_block()` para spans imperativos.
  - Integração com structlog: processor customizado injeta trace_id/span_id
    automaticamente em todos os logs estruturados.

Dependências opcionais:
    pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp
    pip install opentelemetry-instrumentation-httpx  # para auto-instrumentação do httpx

Uso:
    from src.monitoring.tracing import TracingManager, trace_span, get_correlation_id

    # Inicialização (no boot do Orchestrator ou main.py)
    tracing = TracingManager(
        service_name="smart-research-agent",
        jaeger_endpoint="http://localhost:4317",
        export_interval_ms=5000,
    )
    tracing.init()

    # Decorator em qualquer método async
    @trace_span(name="search.github", kind=SpanKind.CLIENT)
    async def search_github(self, query: str):
        ...

    # Context manager imperativo
    async def research(self, query: str):
        with trace_block("pipeline.research", attributes={"query": query}):
            ...
"""

from __future__ import annotations

import functools
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar
from collections.abc import Awaitable

logger = logging.getLogger("monitoring.tracing")

T = TypeVar("T")


# ─── ContextVars para propagation manual (fallback quando OTel não disponível) ──

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_current_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_current_span_id: ContextVar[str | None] = ContextVar("span_id", default=None)


# ─── Enums ───────────────────────────────────────────────────────────────────


class SpanKind(Enum):
    """Tipos de span conforme OpenTelemetry spec."""

    INTERNAL = "internal"
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"


class ExportBackend(Enum):
    """Backends de exportação suportados."""

    JAEGER = "jaeger"
    ZIPKIN = "zipkin"
    OTLP_GRPC = "otlp_grpc"
    OTLP_HTTP = "otlp_http"
    CONSOLE = "console"  # Para debug/dev


# ─── Dataclasses de Configuração ───────────────────────────────────────────


@dataclass
class TracingConfig:
    """Configuração do tracing distribuído.

    Attributes:
        service_name: Nome do serviço no trace (ex: "smart-research-agent").
        service_version: Versão do serviço.
        environment: Ambiente (dev, staging, production).
        jaeger_endpoint: Endpoint OTLP (gRPC) do Jaeger/Collector.
        zipkin_endpoint: Endpoint HTTP do Zipkin.
        backend: Backend preferencial de exportação.
        export_interval_ms: Intervalo de batch export em milissegundos.
        batch_max_queue_size: Tamanho máximo da fila de spans.
        sampler_ratio: Razão de sampling (1.0 = 100%, 0.1 = 10%).
        propagate_to_logs: Se True, injeta trace_id/span_id nos logs structlog.
        enabled: Se False, tracing é no-op (sem overhead).
    """

    service_name: str = "smart-research-agent"
    service_version: str = "1.0.0"
    environment: str = "production"
    jaeger_endpoint: str | None = "http://localhost:4317"
    zipkin_endpoint: str | None = None
    backend: ExportBackend = ExportBackend.JAEGER
    export_interval_ms: int = 5000
    batch_max_queue_size: int = 2048
    sampler_ratio: float = 1.0
    propagate_to_logs: bool = True
    enabled: bool = True


@dataclass
class SpanContext:
    """Contexto de span leve (usado quando OpenTelemetry não está disponível)."""

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    span_name: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    start_time: float | None = None
    end_time: float | None = None
    status: str = "unset"  # unset, ok, error


# ─── TracingManager (Singleton) ────────────────────────────────────────────


class TracingManager:
    """Gerenciador central de tracing distribuído.

    Responsabilidades:
      - Inicializar TracerProvider do OpenTelemetry (se disponível).
      - Configurar exportadores OTLP/Jaeger/Zipkin/Console.
      - Fornecer correlation IDs e span contexts.
      - Integrar com structlog via processor customizado.
    """

    _instance: TracingManager | None = None
    _initialized: bool = False

    def __new__(cls, *args: Any, **kwargs: Any) -> TracingManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config: TracingConfig | None = None):
        if hasattr(self, "_config"):
            return  # Singleton: já inicializado

        self._config = config or TracingConfig()
        self._tracer: Any = None
        self._provider: Any = None
        self._otel_available: bool = False
        self._fallback_spans: list[SpanContext] = []

    # ── Inicialização ───────────────────────────────────────────────────────

    def init(self) -> bool:
        """Inicializa o tracing. Retorna True se OTel foi carregado com sucesso.

        Se OpenTelemetry não estiver instalado, opera em modo fallback
        (correlation IDs + spans em memória, sem exportação).
        """
        if self._initialized:
            return self._otel_available

        if not self._config.enabled:
            logger.info("Tracing desabilitado via configuração.")
            self._initialized = True
            return False

        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.resources import (
                Resource,
                SERVICE_NAME,
                SERVICE_VERSION,
                DEPLOYMENT_ENVIRONMENT,
            )
            from opentelemetry.trace import Status, StatusCode

            self._otel_available = True

            # Resource com metadados do serviço
            resource = Resource.create(
                {
                    SERVICE_NAME: self._config.service_name,
                    SERVICE_VERSION: self._config.service_version,
                    DEPLOYMENT_ENVIRONMENT: self._config.environment,
                }
            )

            # Provider com sampler
            from opentelemetry.sdk.trace import sampling

            sampler = sampling.TraceIdRatioBased(self._config.sampler_ratio)
            self._provider = TracerProvider(resource=resource, sampler=sampler)
            trace.set_tracer_provider(self._provider)
            self._tracer = trace.get_tracer(
                instrumenting_module_name=self._config.service_name,
                instrumenting_library_version=self._config.service_version,
            )

            # Configura exportador
            processor = self._create_span_processor()
            if processor:
                self._provider.add_span_processor(processor)

            # Integração com structlog
            if self._config.propagate_to_logs:
                self._setup_structlog_integration()

            logger.info(
                f"Tracing inicializado: backend={self._config.backend.value}, "
                f"sampler={self._config.sampler_ratio}, endpoint={self._config.jaeger_endpoint or 'N/A'}"
            )

        except ImportError as e:
            logger.warning(
                f"OpenTelemetry não instalado ({e}). Tracing em modo fallback (correlation IDs apenas)."
            )
            self._otel_available = False

        self._initialized = True
        return self._otel_available

    def _create_span_processor(self) -> Any | None:
        """Cria o SpanProcessor apropriado para o backend configurado."""
        if not self._otel_available:
            return None

        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        try:
            if self._config.backend == ExportBackend.OTLP_GRPC:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter as GRPCSpanExporter,
                )

                exporter = GRPCSpanExporter(
                    endpoint=self._config.jaeger_endpoint, insecure=True
                )

            elif self._config.backend == ExportBackend.OTLP_HTTP:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter as HTTPSpanExporter,
                )

                exporter = HTTPSpanExporter(endpoint=self._config.jaeger_endpoint)

            elif self._config.backend == ExportBackend.ZIPKIN:
                from opentelemetry.exporter.zipkin.json import ZipkinExporter

                exporter = ZipkinExporter(
                    endpoint=self._config.zipkin_endpoint
                    or "http://localhost:9411/api/v2/spans"
                )

            elif self._config.backend == ExportBackend.CONSOLE:
                from opentelemetry.sdk.trace.export import ConsoleSpanExporter

                exporter = ConsoleSpanExporter()

            else:  # JAEGER ou default
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                endpoint = self._config.jaeger_endpoint or "http://localhost:4317"
                exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)

            return BatchSpanProcessor(
                exporter,
                max_queue_size=self._config.batch_max_queue_size,
                schedule_delay_millis=self._config.export_interval_ms,
            )

        except ImportError as e:
            logger.warning(
                f"Exportador {self._config.backend.value} não disponível: {e}"
            )
            return None

    def _setup_structlog_integration(self) -> None:
        """Configura processor structlog para injetar trace_id/span_id nos logs."""
        try:
            import structlog

            def trace_context_processor(
                logger: Any, method_name: str, event_dict: dict
            ) -> dict:
                """Processor que injeta trace_id e span_id no dict de log."""
                trace_id = _current_trace_id.get()
                span_id = _current_span_id.get()
                corr_id = _correlation_id.get()

                if trace_id:
                    event_dict["trace_id"] = trace_id
                if span_id:
                    event_dict["span_id"] = span_id
                if corr_id:
                    event_dict["correlation_id"] = corr_id

                return event_dict

            # Adiciona processor ao structlog se ainda não estiver presente
            current_processors = structlog.get_config().get("processors", [])
            if trace_context_processor not in current_processors:
                structlog.configure(
                    processors=current_processors + [trace_context_processor]
                )
                logger.debug(
                    "Structlog integration: trace_context_processor registrado."
                )

        except ImportError:
            logger.debug(
                "structlog não instalado — integração de tracing em logs ignorada."
            )

    def shutdown(self) -> None:
        """Encerra o TracerProvider e flush de spans pendentes."""
        if self._provider and self._otel_available:
            try:
                self._provider.shutdown()
                logger.info("TracingProvider encerrado com sucesso.")
            except Exception as e:
                logger.warning(f"Erro ao encerrar TracingProvider: {e}")
        self._initialized = False
        TracingManager._instance = None

    # ── Propriedades ────────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        return self._otel_available and self._initialized

    @property
    def tracer(self) -> Any:
        return self._tracer

    @property
    def config(self) -> TracingConfig:
        return self._config


# ─── Correlation ID ────────────────────────────────────────────────────────


def get_correlation_id() -> str:
    """Retorna o correlation ID atual ou gera um novo."""
    cid = _correlation_id.get()
    if cid is None:
        cid = str(uuid.uuid4())
        _correlation_id.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    """Define o correlation ID para o contexto atual."""
    _correlation_id.set(cid)


def clear_correlation_id() -> None:
    """Limpa o correlation ID do contexto atual."""
    _correlation_id.set(None)


# ─── Span Helpers ──────────────────────────────────────────────────────────


def get_current_trace_id() -> str | None:
    """Retorna o trace_id do span atual (OTel ou fallback)."""
    if TracingManager().is_available:
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            if span and span.get_span_context().is_valid:
                return format(span.get_span_context().trace_id, "032x")
        except Exception:
            pass
    return _current_trace_id.get()


def get_current_span_id() -> str | None:
    """Retorna o span_id do span atual (OTel ou fallback)."""
    if TracingManager().is_available:
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            if span and span.get_span_context().is_valid:
                return format(span.get_span_context().span_id, "016x")
        except Exception:
            pass
    return _current_span_id.get()


def get_trace_context() -> dict[str, str | None]:
    """Retorna dict com trace_id, span_id e correlation_id atuais."""
    return {
        "trace_id": get_current_trace_id(),
        "span_id": get_current_span_id(),
        "correlation_id": _correlation_id.get(),
    }


# ─── Decorator @trace_span ─────────────────────────────────────────────────


def trace_span(
    name: str | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, Any] | None = None,
    auto_correlation: bool = True,
):
    """Decorator que cria um span OTel (ou fallback) ao redor de uma função async.

    Args:
        name: Nome do span. Se None, usa o nome qualificado da função.
        kind: Tipo de span (CLIENT para searchers/LLMs, INTERNAL para pipeline).
        attributes: Atributos iniciais do span.
        auto_correlation: Se True, gera correlation_id se não existir.

    Uso:
        @trace_span(name="search.github", kind=SpanKind.CLIENT)
        async def search(self, query: str) -> list[SearchResult]:
            ...
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        span_name = name or f"{func.__module__}.{func.__qualname__}"
        attrs = attributes or {}

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            manager = TracingManager()

            # Correlation ID
            if auto_correlation and _correlation_id.get() is None:
                set_correlation_id(str(uuid.uuid4()))

            # OTel mode
            if manager.is_available and manager.tracer:
                from opentelemetry.trace import Status, StatusCode

                otel_kind = _map_span_kind(kind)
                with manager.tracer.start_as_current_span(
                    span_name,
                    kind=otel_kind,
                    attributes=_sanitize_attributes(attrs),
                ) as span:
                    # Injeta trace/span IDs nos contextvars para structlog
                    ctx = span.get_span_context()
                    if ctx.is_valid:
                        _current_trace_id.set(format(ctx.trace_id, "032x"))
                        _current_span_id.set(format(ctx.span_id, "016x"))

                    # Adiciona argumentos como atributos (sanitizados)
                    try:
                        for key, value in kwargs.items():
                            if isinstance(value, (str, int, float, bool)):
                                span.set_attribute(f"arg.{key}", value)
                    except Exception:
                        pass

                    try:
                        result = await func(*args, **kwargs)
                        span.set_status(Status(StatusCode.OK))
                        return result
                    except Exception as e:
                        span.set_status(Status(StatusCode.ERROR, str(e)))
                        span.record_exception(e)
                        raise

            # Fallback mode (sem OTel)
            else:
                span_ctx = SpanContext(
                    trace_id=_current_trace_id.get() or str(uuid.uuid4()),
                    span_id=str(uuid.uuid4())[:16],
                    span_name=span_name,
                    attributes=dict(attrs),
                )
                _current_trace_id.set(span_ctx.trace_id)
                _current_span_id.set(span_ctx.span_id)
                manager._fallback_spans.append(span_ctx)

                import time

                span_ctx.start_time = time.monotonic()
                try:
                    result = await func(*args, **kwargs)
                    span_ctx.status = "ok"
                    return result
                except Exception as e:
                    span_ctx.status = "error"
                    span_ctx.attributes["error"] = str(e)
                    raise
                finally:
                    span_ctx.end_time = time.monotonic()

        return wrapper

    return decorator


# ─── Context Manager trace_block ───────────────────────────────────────────


@contextmanager
def trace_block(
    name: str,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, Any] | None = None,
):
    """Context manager para spans imperativos (síncronos ou async).

    Uso:
        with trace_block("pipeline.research", attributes={"query": query}):
            result = await orchestrator.research(query)

        # Em código async, use 'async with' se precisar de await interno
    """
    manager = TracingManager()
    attrs = attributes or {}

    if manager.is_available and manager.tracer:
        from opentelemetry.trace import Status, StatusCode

        otel_kind = _map_span_kind(kind)
        with manager.tracer.start_as_current_span(
            name,
            kind=otel_kind,
            attributes=_sanitize_attributes(attrs),
        ) as span:
            ctx = span.get_span_context()
            if ctx.is_valid:
                _current_trace_id.set(format(ctx.trace_id, "032x"))
                _current_span_id.set(format(ctx.span_id, "016x"))
            try:
                yield span
                span.set_status(Status(StatusCode.OK))
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise
    else:
        # Fallback
        span_ctx = SpanContext(
            trace_id=_current_trace_id.get() or str(uuid.uuid4()),
            span_id=str(uuid.uuid4())[:16],
            span_name=name,
            attributes=dict(attrs),
        )
        _current_trace_id.set(span_ctx.trace_id)
        _current_span_id.set(span_ctx.span_id)
        manager._fallback_spans.append(span_ctx)

        import time

        span_ctx.start_time = time.monotonic()
        try:
            yield span_ctx
            span_ctx.status = "ok"
        except Exception as e:
            span_ctx.status = "error"
            span_ctx.attributes["error"] = str(e)
            raise
        finally:
            span_ctx.end_time = time.monotonic()


# ─── Helpers internos ──────────────────────────────────────────────────────


def _map_span_kind(kind: SpanKind) -> Any:
    """Mapeia SpanKind interno para o enum do OpenTelemetry."""
    if not TracingManager().is_available:
        return None
    from opentelemetry.trace import SpanKind as OTelSpanKind

    mapping = {
        SpanKind.INTERNAL: OTelSpanKind.INTERNAL,
        SpanKind.SERVER: OTelSpanKind.SERVER,
        SpanKind.CLIENT: OTelSpanKind.CLIENT,
        SpanKind.PRODUCER: OTelSpanKind.PRODUCER,
        SpanKind.CONSUMER: OTelSpanKind.CONSUMER,
    }
    return mapping.get(kind, OTelSpanKind.INTERNAL)


def _sanitize_attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    """Sanitiza atributos para o formato aceito pelo OTel (str, int, float, bool, list)."""
    sanitized: dict[str, Any] = {}
    for key, value in attrs.items():
        if isinstance(value, (str, int, float, bool)):
            sanitized[key] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(v, (str, int, float, bool)) for v in value
        ):
            sanitized[key] = list(value)
        else:
            sanitized[key] = str(value)
    return sanitized


# ─── Funções utilitárias para instrumentação de searchers ───────────────────


def trace_searcher(name: str):
    """Decorator especializado para searchers (CLIENT span + correlation ID)."""
    return trace_span(
        name=name, kind=SpanKind.CLIENT, attributes={"component": "searcher"}
    )


from contextlib import asynccontextmanager


@asynccontextmanager
async def trace_llm(name: str = "llm.completion", *args, **kwargs):
    """Decorator/context manager especializado para chamadas LLM (CLIENT span)."""
    provider = name
    model = args[0] if len(args) > 0 else "unknown"
    task_type = args[1] if len(args) > 1 else "completion"

    attributes = {
        "component": "llm",
        "llm.provider": str(provider),
        "llm.model": str(model),
        "llm.task_type": str(task_type),
    }

    with trace_block(
        name=f"llm.{task_type}", kind=SpanKind.CLIENT, attributes=attributes
    ) as span:
        yield span


def trace_pipeline_stage(stage_name: str):
    """Decorator especializado para stages do pipeline (INTERNAL span)."""
    return trace_span(
        name=f"pipeline.{stage_name}",
        kind=SpanKind.INTERNAL,
        attributes={"component": "pipeline_stage"},
    )


# ─── Aliases de Compatibilidade ──────────────────────────────────────────────
trace_llm_call = trace_llm


# Async context manager especializado para chamadas de busca (CLIENT span).
# Usado por search_service.py como: async with trace_search_call(source, query):
@asynccontextmanager
async def trace_search_call(source_name: str, query: str = ""):
    """Async context manager para instrumentar chamadas de busca (CLIENT span)."""
    attributes = {
        "component": "search",
        "search.source": str(source_name),
        "search.query": str(query),
    }
    with trace_block(
        name=f"search.{source_name}", kind=SpanKind.CLIENT, attributes=attributes
    ) as span:
        yield span


from contextlib import asynccontextmanager


@asynccontextmanager
async def trace_async_span(
    name: str,
    attributes: dict[str, Any] | None = None,
):
    """Context manager assíncrono de compatibilidade que mapeia para trace_block."""
    with trace_block(name, kind=SpanKind.INTERNAL, attributes=attributes) as span:
        yield span


def ensure_correlation_id() -> str:
    """Garante a existência de um correlation ID (alias de get_correlation_id)."""
    return get_correlation_id()


from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Middleware Starlette para injetar e propagar Correlation ID (X-Request-ID)."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        header_name = "X-Request-ID"
        correlation_id = request.headers.get(header_name) or request.headers.get(
            "X-Correlation-ID"
        )
        if not correlation_id:
            correlation_id = str(uuid.uuid4())

        token = _correlation_id.set(correlation_id)
        try:
            response = await call_next(request)
            response.headers[header_name] = correlation_id
            return response
        finally:
            _correlation_id.reset(token)


def setup_tracing(
    service_name: str = "smart-research-agent",
    otlp_endpoint: str | None = None,
    console_export: bool = False,
) -> bool:
    """Configura e inicializa o tracing distribuído (OpenTelemetry)."""
    backend = ExportBackend.CONSOLE if console_export else ExportBackend.OTLP_HTTP
    if otlp_endpoint and otlp_endpoint.startswith("grpc://"):
        backend = ExportBackend.OTLP_GRPC

    config = TracingConfig(
        service_name=service_name,
        jaeger_endpoint=otlp_endpoint,
        backend=backend,
        enabled=True,
    )
    manager = TracingManager(config)
    return manager.init()
