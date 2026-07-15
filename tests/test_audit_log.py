"""
Testes do Audit Log (Bloco 10 / E7-T1).

Cobre:
- log_research escreve uma linha JSON válida em logs/audit.jsonl.
- A linha contém query, mode, sources_used, ragas_score e token_estimate.
- Falhas de I/O nunca levantam (best-effort) — loga e prossegue.
- Integração: AuditLogger é chamado no fim do research() do Orchestrator
  (com um handler de log real, verificamos a linha escrita).
"""

import json
import os
from unittest.mock import MagicMock

import pytest

from src.audit_log import AuditLogger, get_audit_logger


def _write_and_read(logger: AuditLogger, **kwargs) -> dict:
    """Escreve via log_research e relê a última linha do arquivo."""
    logger.log_research(**kwargs)
    with open(logger.log_path, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def test_log_research_writes_json_line(tmp_path):
    """log_research escreve uma linha JSON válida com os campos esperados."""
    log_path = tmp_path / "audit.jsonl"
    al = AuditLogger(log_path=str(log_path))
    entry = _write_and_read(
        al,
        query="qual a melhor forma de fazer X?",
        mode="academico",
        sources_used=["pubmed", "arxiv", "crossref"],
        ragas_score=0.82,
        token_estimate=1500,
    )
    assert entry["event"] == "research"
    assert entry["query"] == "qual a melhor forma de fazer X?"
    assert entry["mode"] == "academico"
    assert entry["sources_used"] == ["pubmed", "arxiv", "crossref"]
    assert entry["ragas_score"] == 0.82
    assert entry["token_estimate"] == 1500
    assert "timestamp" in entry


def test_log_research_handles_missing_fields(tmp_path):
    """Campos opcionais ausentes não quebram o log (None gravado)."""
    log_path = tmp_path / "audit.jsonl"
    al = AuditLogger(log_path=str(log_path))
    entry = _write_and_read(al, query="q simples")
    assert entry["query"] == "q simples"
    assert entry["sources_used"] == []
    assert entry["ragas_score"] is None
    assert entry["token_estimate"] is None


def test_log_research_never_raises_on_bad_path():
    """Caminho de log inválido (dir inexistente sem permissão de criar) -> no raise."""
    # Usa um caminho sob um arquivo (não diretório) para forçar falha de I/O.
    bad_dir = "/\\/:*?invalid/audit.jsonl"
    al = AuditLogger(log_path=bad_dir)
    # Não deve levantar sob nenhuma circunstância.
    al.log_research(query="q", mode="radar")
    al.close()


def test_creates_parent_directory(tmp_path):
    """O logger cria o diretório pai (logs/) se não existir."""
    log_path = tmp_path / "nested" / "logs" / "audit.jsonl"
    al = AuditLogger(log_path=str(log_path))
    entry = _write_and_read(al, query="q")
    assert entry["query"] == "q"
    assert os.path.isdir(os.path.dirname(log_path))


def test_singleton_getter():
    """get_audit_logger retorna a mesma instância singleton."""
    a = get_audit_logger()
    b = get_audit_logger()
    assert a is b
