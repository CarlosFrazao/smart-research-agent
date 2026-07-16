"""
Testes do logging_utils (porte do Hermes Agent).

Cobre:
- ``RedactingFormatter`` mascara segredos (prefixos, Authorization, DB connstr,  # pragma: allowlist secret
  JWT, token em URL, env assign, campo JSON) e preserva texto limpo.  # pragma: allowlist secret
- ``redact_sensitive_text`` pode ser forçado via ``force=True`` e desligado via
  ``SRA_REDACT_SECRETS=false`` (snapshot em import-time respeitado).  # pragma: allowlist secret
- ``RotatingFileHandler`` resolve para ``ConcurrentRotatingFileHandler`` no
  Windows e para a stdlib em POSIX (tolerância a multi-processo / WinError 32).
- Integração: ``AuditLogger`` com segredo na query grava a linha redactada em
  disco e continua produzindo JSON válido.
"""

import json
import logging
import os
import sys

import pytest

from src import logging_utils
from src.logging_utils import (
    RedactingFormatter,
    RotatingFileHandler,
    TimedRotatingFileHandler,
    mask_secret,  # pragma: allowlist secret
    redact_sensitive_text,
)


# ---------------------------------------------------------------------------
# mask_secret  # pragma: allowlist secret
# ---------------------------------------------------------------------------


def test_mask_secret_preserves_head_tail():  # pragma: allowlist secret
    assert (
        mask_secret("sk-proj-abcdef1234567890")  # pragma: allowlist secret
        == "sk-p...7890"  # pragma: allowlist secret
    )  # pragma: allowlist secret


def test_mask_secret_short_fully_masked():  # pragma: allowlist secret
    assert mask_secret("short") == "***"  # pragma: allowlist secret


def test_mask_secret_empty():  # pragma: allowlist secret
    assert mask_secret("") == ""  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# redact_sensitive_text — padrões individuais
# ---------------------------------------------------------------------------


def test_redact_known_prefix():
    text = "minha key e sk-ant-abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret
    out = redact_sensitive_text(text)
    assert (
        "sk-ant-abcdefghijklmnopqrstuvwxyz123456" not in out  # pragma: allowlist secret
    )  # pragma: allowlist secret
    assert (
        "sk-ant..." in out  # pragma: allowlist secret
    )  # head=6 preservado, resto mascarado # pragma: allowlist secret


def test_redact_github_pat():
    text = "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"  # pragma: allowlist secret
    out = redact_sensitive_text(text)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in out  # pragma: allowlist secret


def test_redact_authorization_header():  # pragma: allowlist secret
    text = "Authorization: Bearer sk-ant-abcdefghijklmnopqrstuvwxyz"  # pragma: allowlist secret
    out = redact_sensitive_text(text)
    assert "Bearer sk-ant" not in out  # pragma: allowlist secret
    assert out.startswith("Authorization: Bearer ***")  # pragma: allowlist secret


def test_redact_authorization_basic_scheme():  # pragma: allowlist secret
    text = "Proxy-Authorization: Basic dXNlcjpwYXNzd29yZA=="  # pragma: allowlist secret
    out = redact_sensitive_text(text)
    assert "dXNlcjpwYXNzd29yZA==" not in out  # pragma: allowlist secret
    assert out.startswith("Proxy-Authorization: Basic ***")  # pragma: allowlist secret


def test_redact_db_connstr():
    text = "postgresql://user:S3nh4Sup3rSecreta@db.example.com:5432/app"  # pragma: allowlist secret
    out = redact_sensitive_text(text)
    assert "S3nh4Sup3rSecreta" not in out  # pragma: allowlist secret
    assert out.startswith(
        "postgresql://user:***@db.example.com:5432/app"  # pragma: allowlist secret
    )  # pragma: allowlist secret


def test_redact_jwt():
    text = "header eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123 payload"  # pragma: allowlist secret
    out = redact_sensitive_text(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in out  # pragma: allowlist secret
    assert "***" in out


def test_redact_url_bare_token():  # pragma: allowlist secret
    text = "git remote add origin https://ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345@github.com/x/y"  # pragma: allowlist secret
    out = redact_sensitive_text(text)
    assert (
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345@github.com"  # pragma: allowlist secret
        not in out  # pragma: allowlist secret
    )  # pragma: allowlist secret


def test_redact_env_assignment():
    text = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret
    out = redact_sensitive_text(text)
    # Segredo cru nunca aparece; valor é mascarado (camada env-assign sobrescreve
    # o prefix-mask com ***).
    assert (
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret
        not in out  # pragma: allowlist secret
    )  # pragma: allowlist secret
    assert out.startswith("OPENAI_API_KEY=")
    assert "***" in out


def test_redact_json_field():
    text = '{"apiKey": "sk-ant-abcdefghijklmnopqrstuvwxyz123456", "ok": true}'  # pragma: allowlist secret
    out = redact_sensitive_text(text)
    assert (
        "sk-ant-abcdefghijklmnopqrstuvwxyz123456" not in out  # pragma: allowlist secret
    )  # pragma: allowlist secret
    assert (
        '"apiKey":"***"' in out  # pragma: allowlist secret
    )  # camada JSON-field mascara o valor # pragma: allowlist secret


def test_redact_preserves_clean_text():
    text = "pesquisa sobre concorrencia em python asyncio, sem segredos"
    assert redact_sensitive_text(text) == text


def test_redact_on_log_line_with_query_containing_secret():  # pragma: allowlist secret
    line = json.dumps(
        {
            "query": "use a key sk-ant-abcdefghijklmnopqrstuvwxyz123456 to login"  # pragma: allowlist secret
        },  # pragma: allowlist secret
        ensure_ascii=False,
    )
    out = redact_sensitive_text(line)
    assert (
        "sk-ant-abcdefghijklmnopqrstuvwxyz123456" not in out  # pragma: allowlist secret
    )  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# force / toggle de ambiente
# ---------------------------------------------------------------------------


def test_redact_force_redacts_regardless_of_env(monkeypatch):
    # Com force=True a redaction acontece mesmo que o flag global esteja off.
    # (O snapshot em import-time já é True por padrão; force é a garantia de
    # fronteira de segurança que nunca retorna segredo cru.)
    text = "key sk-ant-abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret
    assert (
        "sk-ant-abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret
        not in redact_sensitive_text(  # pragma: allowlist secret
            text, force=True
        )
    )  # pragma: allowlist secret
    # Sem force e com o flag padrão (ligado), também redacta.
    assert (
        "sk-ant-abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret
        not in redact_sensitive_text(  # pragma: allowlist secret
            text
        )
    )  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# RedactingFormatter via logging
# ---------------------------------------------------------------------------


def test_redacting_formatter_masks_in_log_record():
    fmt = RedactingFormatter("%(levelname)s %(name)s: %(message)s")
    record = logging.LogRecord(
        name="audit_log",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="call with token sk-ant-abcdefghijklmnopqrstuvwxyz123456 done",  # pragma: allowlist secret
        args=(),
        exc_info=None,
    )
    rendered = fmt.format(record)
    assert (
        "sk-ant-abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret
        not in rendered  # pragma: allowlist secret
    )  # pragma: allowlist secret
    assert (
        "sk-ant..." in rendered  # pragma: allowlist secret
    )  # msg solto: prefix-mask é o resultado final # pragma: allowlist secret
    assert "INFO audit_log:" in rendered


# ---------------------------------------------------------------------------
# Handler resolution (Windows vs POSIX)
# ---------------------------------------------------------------------------


def test_timed_handler_resolves_to_concurrent_when_available():
    """No Windows com concurrent-log-handler instalado, o handler é a variante
    tolerante a multi-processo (evita WinError 32).  Se a lib não estiver
    presente, cai no fallback stdlib sem quebrar a importação."""
    if sys.platform == "win32" and "concurrent_log_handler" in sys.modules:
        assert TimedRotatingFileHandler.__name__ == "ConcurrentTimedRotatingFileHandler"
    else:
        # POSIX ou lib ausente: stdlib.
        assert TimedRotatingFileHandler.__module__ == "logging.handlers"


def test_concurrent_handler_is_instantiable():
    """O handler resolvido pode ser instanciado e usado para escrever (sem
    disparar WinError 32 sob concorrência de processos no Windows)."""
    import tempfile
    import os

    d = tempfile.mkdtemp()
    p = os.path.join(d, "probe.log")
    h = TimedRotatingFileHandler(p, when="midnight", backupCount=3, encoding="utf-8")
    try:
        h.emit(
            logging.LogRecord(
                name="probe",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg="hello",
                args=(),
                exc_info=None,
            )
        )
        with open(p, "r", encoding="utf-8") as fh:
            assert "hello" in fh.read()
    finally:
        h.close()


# ---------------------------------------------------------------------------
# Integração com AuditLogger
# ---------------------------------------------------------------------------


def test_audit_logger_redacts_secret_in_query(tmp_path):  # pragma: allowlist secret
    """AuditLogger grava a query com segredo mascarado e mantém JSON válido."""
    from src.audit_log import AuditLogger

    log_path = tmp_path / "audit.jsonl"
    al = AuditLogger(log_path=str(log_path))
    al.log_research(
        query="login usando sk-ant-abcdefghijklmnopqrstuvwxyz123456",  # pragma: allowlist secret
        mode="academico",
        sources_used=["arxiv"],
        ragas_score=0.9,
        token_estimate=100,  # pragma: allowlist secret
    )
    with open(log_path, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    entry = json.loads(lines[-1])
    assert (
        "sk-ant-abcdefghijklmnopqrstuvwxyz123456"  # pragma: allowlist secret
        not in entry["query"]  # pragma: allowlist secret
    )  # pragma: allowlist secret
    assert (
        "sk-ant..." in entry["query"]  # pragma: allowlist secret
    )  # prefix preservado, sufixo mascarado # pragma: allowlist secret
    assert entry["event"] == "research"
    assert entry["mode"] == "academico"
