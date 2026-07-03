from __future__ import annotations
import logging
import sys
from typing import Optional

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