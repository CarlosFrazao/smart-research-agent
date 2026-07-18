"""Testes do Épico D (D1/F6): deduplicação de handlers de logging + retries em DEBUG.

Critério de aceite D1: "Log sem linhas duplicadas; retries em DEBUG".
"""

import logging
import os

import pytest

from src.logging_utils import (
    CorrelationIdFilter,
    RedactingFormatter,
    configure_root_logger,
    dedupe_handlers,
    dedupe_root_handlers,
)


def _fresh_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    for h in list(log.handlers):
        log.removeHandler(h)
    log.setLevel(logging.DEBUG)
    return log


def test_dedupe_removes_duplicate_streamhandlers():
    log = _fresh_logger("sra_test_dedupe_1")
    log.addHandler(logging.StreamHandler())
    log.addHandler(logging.StreamHandler())
    log.addHandler(logging.StreamHandler())
    assert len(log.handlers) == 3
    removed = dedupe_handlers(log)
    assert removed == 2
    assert len(log.handlers) == 1


def test_dedupe_keeps_stdout_and_stderr_separate():
    log = _fresh_logger("sra_test_dedupe_2")
    import sys

    log.addHandler(logging.StreamHandler(sys.stdout))
    log.addHandler(logging.StreamHandler(sys.stderr))
    removed = dedupe_handlers(log)
    assert removed == 0
    assert len(log.handlers) == 2


def test_dedupe_keeps_distinct_filehandlers():
    log = _fresh_logger("sra_test_dedupe_3")
    p1 = os.path.join("logs", "a.log")
    p2 = os.path.join("logs", "b.log")
    h1 = logging.FileHandler(p1, encoding="utf-8")
    h2 = logging.FileHandler(p2, encoding="utf-8")
    log.addHandler(h1)
    log.addHandler(h2)
    assert dedupe_handlers(log) == 0
    assert len(log.handlers) == 2
    h1.close()
    h2.close()


def test_configure_root_logger_single_stdout(monkeypatch):
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    root = configure_root_logger("INFO")
    streams = [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    assert len(streams) == 1
    # Idempotente: chamar de novo não duplica.
    configure_root_logger("INFO")
    streams2 = [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    assert len(streams2) == 1


def test_configure_root_logger_adds_single_file(monkeypatch, tmp_path):
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    log_file = tmp_path / "sra.log"
    root = configure_root_logger("DEBUG", log_file=str(log_file))
    files = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    streams = [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    assert len(files) == 1
    assert len(streams) == 1
    # Idempotente com mesmo arquivo.
    configure_root_logger("DEBUG", log_file=str(log_file))
    files2 = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(files2) == 1


def test_correlation_id_filter_injects_default():
    f = CorrelationIdFilter()
    record = logging.LogRecord(
        "n", logging.INFO, "p", 1, "m", None, None
    )
    assert f.filter(record) is True
    assert record.correlation_id == "-"


def test_redacting_formatter_masks_secret(capsys):
    fmt = RedactingFormatter("%(message)s")
    rec = logging.LogRecord("n", logging.INFO, "p", 1, "key sk-abcd1234567890xyz", None, None)
    out = fmt.format(rec)
    assert "sk-abcd1234567890xyz" not in out
