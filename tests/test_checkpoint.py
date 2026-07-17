"""Tests for src/pipeline/checkpoint.py (FEAT-005)."""

from __future__ import annotations

import json

from src.pipeline.checkpoint import DeepCheckpoint, checkpoint_every


def _state(steps: int = 3) -> dict:
    return {
        "query": "best vector db for local RAG",
        "steps_done": steps,
        "draft": "## Draft\nPartial findings...",
    }


def test_save_then_load_restores_state(tmp_path):
    ck = DeepCheckpoint(base_dir=str(tmp_path / ".sra_checkpoints"))
    assert ck.save("run-1", _state(3)) is True
    loaded = ck.load("run-1")
    assert loaded is not None
    assert loaded["steps_done"] == 3
    assert loaded["query"] == "best vector db for local RAG"
    assert "ts" in loaded


def test_load_missing_run_returns_none(tmp_path):
    ck = DeepCheckpoint(base_dir=str(tmp_path / ".sra_checkpoints"))
    assert ck.load("never-saved") is None


def test_load_corrupt_json_returns_none_and_logs(tmp_path):
    ck = DeepCheckpoint(base_dir=str(tmp_path / ".sra_checkpoints"))
    path = tmp_path / ".sra_checkpoints" / "broken.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")
    assert ck.load("broken") is None


def test_redact_applied_on_save(tmp_path):
    ck = DeepCheckpoint(base_dir=str(tmp_path / ".sra_checkpoints"))
    secret_state = {"query": "sk-abcd1234EFGH5678IJKL9012mnop", "steps_done": 1, "draft": "x"}  # pragma: allowlist secret
    ck.save("secret-run", secret_state)
    path = tmp_path / ".sra_checkpoints" / "secret-run.json"
    raw = path.read_text(encoding="utf-8")
    assert "sk-abcd1234EFGH5678IJKL9012mnop" not in raw  # pragma: allowlist secret
    # Redaction keeps original value in memory untouched.
    assert secret_state["query"] == "sk-abcd1234EFGH5678IJKL9012mnop"  # pragma: allowlist secret


def test_resume_after_partial_save(tmp_path):
    ck = DeepCheckpoint(base_dir=str(tmp_path / ".sra_checkpoints"))
    ck.save("resume", _state(6))
    ck.save("resume", _state(10))
    loaded = ck.load("resume")
    assert loaded["steps_done"] == 10


def test_invalid_run_id_rejected(tmp_path):
    ck = DeepCheckpoint(base_dir=str(tmp_path / ".sra_checkpoints"))
    for bad in ("../escape", "a\\b", ".hidden"):
        try:
            ck.save(bad, _state())
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for run_id {bad!r}")
        assert ck.load(bad) is None


def test_checkpoint_every_default_and_env(monkeypatch):
    monkeypatch.delenv("SRA_CHECKPOINT_EVERY", raising=False)
    assert checkpoint_every() == 5
    monkeypatch.setenv("SRA_CHECKPOINT_EVERY", "2")
    assert checkpoint_every() == 2
    monkeypatch.setenv("SRA_CHECKPOINT_EVERY", "0")
    assert checkpoint_every() == 5
    monkeypatch.setenv("SRA_CHECKPOINT_EVERY", "not-a-number")
    assert checkpoint_every() == 5


def test_state_payload_is_valid_json(tmp_path):
    ck = DeepCheckpoint(base_dir=str(tmp_path / ".sra_checkpoints"))
    ck.save("json-run", _state(2))
    path = tmp_path / ".sra_checkpoints" / "json-run.json"
    # Must round-trip as JSON without error.
    assert json.loads(path.read_text(encoding="utf-8"))["steps_done"] == 2
