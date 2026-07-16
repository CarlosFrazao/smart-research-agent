"""
Audit Log (Bloco 10 / E7-T1) — Registro de auditoria de pesquisas.

Mantém um log append-only de cada pesquisa executada pelo SRA em
``logs/audit.jsonl`` (um objeto JSON por linha), com rotação diária e
retenção de 30 dias. O objetivo é prover rastreabilidade forense (qual
query, modo, fontes, score RAGAS e custo estimado de tokens) sem
dependências externas (apenas stdlib + concurrent-log-handler no Windows).

O ``AuditLogger`` é **best-effort**: qualquer falha de I/O (disco cheio,
sem permissão) é logada e nunca aborta o pipeline de pesquisa — a
auditoria é um observador, não um participante crítico do fluxo.

Aprimoramentos de logging (portados do Hermes Agent):
  * Handler de rotação tolerante a Windows — evita ``WinError 32`` quando
    múltiplos processos escrevem no mesmo ``audit.jsonl`` (usa
    ``ConcurrentRotatingFileHandler`` no Windows, stdlib no POSIX).
  * ``RedactingFormatter`` — mascara segredos (API keys, tokens) antes de
    escrevê-los em disco, alinhado ao Bloco E7-T1 (detect-secrets).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from src.logging_utils import TimedRotatingFileHandler, redact_sensitive_text

logger = logging.getLogger("audit_log")

# Caminho padrão do log (relativo à raiz do projeto; logs/ é gitignored).
_DEFAULT_LOG_PATH = os.path.join("logs", "audit.jsonl")
_RETENTION_DAYS = 30


class AuditLogger:
    """Registra pesquisas em ``logs/audit.jsonl`` com rotação diária.

    Args:
        log_path: Caminho do arquivo de auditoria (default ``logs/audit.jsonl``).
        retention_days: Dias de retenção antes da rotação descartar backups.
    """

    def __init__(
        self, log_path: str = _DEFAULT_LOG_PATH, retention_days: int = _RETENTION_DAYS
    ) -> None:
        self.log_path = log_path
        self.retention_days = retention_days
        self._handler: TimedRotatingFileHandler | None = None
        self._lock = threading.Lock()
        self._ensure_handler()

    def _ensure_handler(self) -> None:
        """Cria o handler de rotação uma única vez (lazy, gracioso).

        Usa ``TimedRotatingFileHandler`` tolerante a Windows (portado do
        Hermes): no Windows ele serializa a rotação com um lock cross-process
        para evitar ``PermissionError [WinError 32]`` quando vários processos
        escrevem no mesmo arquivo.  Os segredos são mascarados antes da
        escrita via ``redact_sensitive_text`` (Bloco E7-T1), preservando o
        contrato de uma linha JSON válida por registro.
        """
        if self._handler is not None:
            return
        try:
            parent = os.path.dirname(self.log_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            handler = TimedRotatingFileHandler(
                self.log_path,
                when="midnight",
                backupCount=self.retention_days,
                encoding="utf-8",
            )
            self._handler = handler
        except Exception as exc:  # pragma: no cover - defensivo
            logger.warning("AuditLogger: falha ao criar handler de log: %s", exc)
            self._handler = None

    def log_research(
        self,
        query: str,
        mode: str = "",
        sources_used: list[str] | None = None,
        ragas_score: float | None = None,
        token_estimate: int | None = None,
    ) -> None:
        """Registra uma pesquisa concluída como uma linha JSON.

        Args:
            query: Query pesquisada pelo usuário.
            mode: Modo de operação (ex.: "academico", "radar").
            sources_used: Lista de fontes/searchers consultados.
            ragas_score: Score de qualidade RAGAS (faithfulness/relevancy
                agregado), se disponível.
            token_estimate: Estimativa de tokens consumidos, se disponível.

        A operação é thread-safe e nunca levanta — falhas são logadas.
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "research",
            "query": query,
            "mode": mode,
            "sources_used": list(sources_used or []),
            "ragas_score": ragas_score,
            "token_estimate": token_estimate,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        # Mascara segredos (API keys, tokens) antes de persistir — Bloco E7-T1.
        safe_line = redact_sensitive_text(line)

        with self._lock:
            if self._handler is None:
                self._ensure_handler()
            handler = self._handler
            if handler is None:
                # Fallback: append direto se o handler falhou na criação.
                try:
                    parent = os.path.dirname(self.log_path)
                    if parent and not os.path.isdir(parent):
                        os.makedirs(parent, exist_ok=True)
                    with open(self.log_path, "a", encoding="utf-8") as fh:
                        fh.write(safe_line + "\n")
                except Exception as exc:  # pragma: no cover - defensivo
                    logger.warning("AuditLogger: falha ao escrever log: %s", exc)
                return
            try:
                handler.emit(
                    logging.LogRecord(
                        name="audit_log",
                        level=logging.INFO,
                        pathname=__file__,
                        lineno=0,
                        msg=safe_line,
                        args=(),
                        exc_info=None,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensivo
                logger.warning("AuditLogger: falha ao emitir log: %s", exc)

    def close(self) -> None:
        """Fecha o handler de rotação (chamado no shutdown do orquestrador)."""
        with self._lock:
            if self._handler is not None:
                try:
                    self._handler.close()
                except Exception:  # pragma: no cover - defensivo
                    pass
                self._handler = None


# Instância singleton compartilhada por todos os entry points (best-effort).
_default_logger: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    """Retorna a instância singleton do ``AuditLogger`` (lazy)."""
    global _default_logger
    if _default_logger is None:
        _default_logger = AuditLogger()
    return _default_logger
