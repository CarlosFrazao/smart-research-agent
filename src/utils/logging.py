from __future__ import annotations
import logging
import sys
import os
import json
from datetime import datetime
from typing import Optional

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
        formatter = ColoredFormatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
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
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception:
            pass  # Fail-safe

    def log_search(self, source: str, query: str, results_count: int, error: str = None):
        self._write_log({
            "event": "search",
            "source": source,
            "query": query,
            "results_count": results_count,
            "error": error
        })

    def log_gap(self, gap_description: str, query_used: str, iteration: int):
        self._write_log({
            "event": "gap_detection",
            "gap_description": gap_description,
            "query_used": query_used,
            "iteration": iteration
        })
        
    def log_event(self, event_name: str, **kwargs):
        self._write_log({
            "event": event_name,
            **kwargs
        })

structured_logger = StructuredLogger()


def setup_logging(
    level: str = "INFO",
    json_output: bool = True,
    log_file: Optional[str] = None,
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
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
        ]

        if json_output:
            processors = shared_processors + [structlog.processors.JSONRenderer()]
        else:
            processors = shared_processors + [structlog.dev.ConsoleRenderer(colors=True)]

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
        logging.basicConfig(
            format="%(message)s",
            stream=sys.stdout,
            level=numeric_level
        )
        
    except ImportError:
        # Fallback se structlog não estiver no virtualenv
        log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        if log_file:
            logging.basicConfig(
                level=numeric_level,
                format=log_format,
                filename=log_file,
                filemode="a",
                encoding="utf-8"
            )
        else:
            logging.basicConfig(
                level=numeric_level,
                format=log_format,
                stream=sys.stdout
            )
        logger = logging.getLogger(__name__)
        logger.warning("structlog não está instalado. Usando fallback do logging padrão.")