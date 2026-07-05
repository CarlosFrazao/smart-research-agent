"""Utilitarios de configuracao de logging estruturado com suporte a JSON e niveis de log dinamicos.

Todos os logs emitidos por este modulo (via `setup_logger`, `setup_logging` ou
`structured_logger`) carregam automaticamente o `correlation_id` da requisicao/
pesquisa atual — e, quando o tracing distribuido estiver habilitado (ver
`src.monitoring.tracing`), tambem `trace_id`/`span_id` do span ativo. Isso
permite correlacionar uma linha de log com o span exato do pipeline (etapa de
busca, chamada LLM, etc.) que a gerou, sem que nenhum call-site precise mudar.

A integracao com `src.monitoring.tracing` é sempre feita via import tardio e
protegida por try/except: se o modulo de tracing nao estiver disponivel por
qualquer motivo, o logging continua funcionando normalmente, apenas sem os
campos de correlacao (mesma filosofia de fallback do resto do projeto).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from contextvars import ContextVar
from typing import Any

# Força UTF-8 nos fluxos padrão para evitar UnicodeEncodeError no Windows (CP1252)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Fallback context variables dictionary
_fallback_log_context: ContextVar[dict[str, Any]] = ContextVar("fallback_log_context", default={})

def bind_contextvars(**kwargs: Any) -> None:
    """Fallback / Wrapper para associar variáveis de contexto ao thread/corrotina atual."""
    try:
        import structlog
        structlog.contextvars.bind_contextvars(**kwargs)
    except ImportError:
        ctx = _fallback_log_context.get().copy()
        ctx.update(kwargs)
        _fallback_log_context.set(ctx)

def clear_contextvars() -> None:
    """Fallback / Wrapper para limpar variáveis de contexto."""
    try:
        import structlog
        structlog.contextvars.clear_contextvars()
    except ImportError:
        _fallback_log_context.set({})

def get_contextvars() -> dict[str, Any]:
    """Fallback / Wrapper para recuperar variáveis de contexto."""
    try:
        import structlog
        return structlog.contextvars.get_contextvars()
    except (ImportError, AttributeError):
        return _fallback_log_context.get()


def _current_trace_fields() -> dict[str, str]:
    """Coleta correlation_id/trace_id/span_id do contexto atual.

    Import tardio e protegido: `src.monitoring.tracing` nunca é uma dependência
    obrigatória para que o logging funcione.
    """
    fields: dict[str, str] = {}
    try:
        from src.monitoring.tracing import get_correlation_id, get_current_trace_context

        correlation_id = get_correlation_id()
        if correlation_id:
            fields["correlation_id"] = correlation_id
        fields.update(get_current_trace_context())
    except Exception:
        pass
        
    try:
        fields.update({k: str(v) for k, v in get_contextvars().items()})
    except Exception:
        pass
        
    return fields


class CorrelationIdFilter(logging.Filter):
    """Filtro que injeta `correlation_id`/`trace_id`/`span_id` em todo `LogRecord`.

    Usa `-` como valor padrão quando não há contexto de correlação ativo, para
    que a string de formato (`%(correlation_id)s`) nunca falhe por atributo
    ausente (ex: logs emitidos fora de uma requisição/pesquisa, como no boot
    da aplicação).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        fields = _current_trace_fields()
        record.correlation_id = fields.get("correlation_id", "-")
        record.trace_id = fields.get("trace_id", "-")
        record.span_id = fields.get("span_id", "-")
        return True


class ColoredFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


def setup_logger(name: str = "smart_research", level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        handler.addFilter(CorrelationIdFilter())
        formatter = ColoredFormatter(
            "%(asctime)s [%(levelname)s] %(name)s [corr=%(correlation_id)s]: %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


class StructuredLogger:
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_file = os.path.join(self.log_dir, "sra_structured.jsonl")

    def _write_log(self, data: dict):
        try:
            data["timestamp"] = datetime.now().isoformat()
            data.update(_current_trace_fields())
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception:
            pass  # Fail-safe

    def log_search(
        self, source: str, query: str, results_count: int, error: str = None
    ):
        self._write_log(
            {
                "event": "search",
                "source": source,
                "query": query,
                "results_count": results_count,
                "error": error,
            }
        )

    def log_gap(self, gap_description: str, query_used: str, iteration: int):
        self._write_log(
            {
                "event": "gap_detection",
                "gap_description": gap_description,
                "query_used": query_used,
                "iteration": iteration,
            }
        )

    def log_event(self, event_name: str, **kwargs):
        self._write_log({"event": event_name, **kwargs})


structured_logger = StructuredLogger()


def _bind_trace_context(logger, method_name, event_dict):
    """Processor structlog que injeta correlation_id/trace_id/span_id no evento."""
    event_dict.update(_current_trace_fields())
    return event_dict


def setup_logging(
    level: str = "INFO",
    json_output: bool = True,
    log_file: str | None = None,
) -> None:
    """
    Configura o logging estruturado.
    Caso a biblioteca structlog não esteja instalada no ambiente de execução,
    realiza fallback seguro para o logging padrão do Python.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    try:
        import structlog

        shared_processors = [
            structlog.contextvars.merge_contextvars,
            _bind_trace_context,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
        ]

        if json_output:
            processors = shared_processors + [structlog.processors.JSONRenderer()]
        else:
            processors = shared_processors + [
                structlog.dev.ConsoleRenderer(colors=True)
            ]

        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(
                file=open(log_file, "a", encoding="utf-8") if log_file else sys.stdout
            ),
            cache_logger_on_first_use=True,
        )

        # Redireciona logs tradicionais do Python para o structlog
        root_handler = logging.StreamHandler(sys.stdout)
        root_handler.addFilter(CorrelationIdFilter())
        logging.basicConfig(
            format="%(message)s",
            level=numeric_level,
            handlers=[root_handler],
            force=True,
        )

    except ImportError:
        # Fallback se structlog não estiver no virtualenv
        log_format = "%(asctime)s [%(levelname)s] %(name)s [corr=%(correlation_id)s]: %(message)s"
        handler = (
            logging.FileHandler(log_file, encoding="utf-8")
            if log_file
            else logging.StreamHandler(sys.stdout)
        )
        handler.addFilter(CorrelationIdFilter())
        handler.setFormatter(logging.Formatter(log_format))
        logging.basicConfig(level=numeric_level, handlers=[handler], force=True)
        logger = logging.getLogger(__name__)
        logger.warning(
            "structlog não está instalado. Usando fallback do logging padrão."
        )
