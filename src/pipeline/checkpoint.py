"""Deep research checkpointing (FEAT-005).

Provides :class:`DeepCheckpoint`, a tiny JSON-backed persistence layer that lets
the deep researcher resume after a crash. State is serialized to
``.sra_checkpoints/<run_id>.json`` and redacted before being written.

Design goals (PRD 4.5):
- ``save(run_id, state)`` persists a serializable dict every ``CHECKPOINT_EVERY``
  steps (default 5, overridable via ``SRA_CHECKPOINT_EVERY``).
- ``load(run_id)`` returns the last saved state, or ``None`` when no checkpoint
  exists or the file is unreadable/corrupt (graceful fresh start).
- Writes are protected by a file lock with bounded retries to survive
  ``WinError 32`` (file in use) on Windows.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from src.logging_utils import redact_sensitive_text

_CHECKPOINT_DIRNAME = ".sra_checkpoints"
_DEFAULT_EVERY = 5
_MAX_WRITE_RETRIES = 5
_RETRY_BACKOFF_S = 0.1


def checkpoint_every() -> int:
    """Return the step cadence for checkpointing.

    Reads ``SRA_CHECKPOINT_EVERY`` (default 5). Invalid values fall back to the
    default instead of raising.
    """
    raw = os.getenv("SRA_CHECKPOINT_EVERY")
    if not raw:
        return _DEFAULT_EVERY
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_EVERY
    if value < 1:
        return _DEFAULT_EVERY
    return value


class DeepCheckpoint:
    """JSON-backed checkpoint store for a deep research run."""

    def __init__(self, base_dir: str = _CHECKPOINT_DIRNAME) -> None:
        """Create the checkpoint manager rooted at ``base_dir``.

        Args:
            base_dir: Directory (relative or absolute) holding checkpoint files.
        """
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        """Resolve the on-disk path for a run id, rejecting traversal."""
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            raise ValueError(f"invalid run_id: {run_id!r}")
        return self.base_dir / f"{run_id}.json"

    def _redact(self, state: dict[str, Any]) -> dict[str, Any]:
        """Return a redacted copy of ``state`` safe to persist.

        String values are passed through ``redact_sensitive_text``. The original
        ``state`` is never mutated.
        """
        redacted: dict[str, Any] = {}
        for key, value in state.items():
            if isinstance(value, str):
                redacted[key] = redact_sensitive_text(value)
            else:
                redacted[key] = value
        return redacted

    def save(self, run_id: str, state: dict[str, Any]) -> bool:
        """Persist ``state`` for ``run_id``.

        A timestamp is stamped into the payload under ``ts`` when absent. Writes
        are retried on ``OSError`` (e.g. Windows file-lock) up to
        ``_MAX_WRITE_RETRIES`` attempts.

        Args:
            run_id: Stable identifier for the run.
            state: Serializable dict (typically ``{query, steps_done, draft}``).

        Returns:
            True if the checkpoint was written, False on persistent failure.
        """
        if not isinstance(state, dict):
            raise TypeError("checkpoint state must be a dict")
        payload = self._redact(dict(state))
        if "ts" not in payload:
            payload["ts"] = time.time()
        path = self._path(run_id)
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        last_error: Optional[Exception] = None
        for attempt in range(_MAX_WRITE_RETRIES):
            try:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(data, encoding="utf-8")
                tmp.replace(path)
                return True
            except OSError as exc:
                last_error = exc
                time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
        # Non-recoverable write failure: log and degrade gracefully.
        print(f"[checkpoint] failed to save run '{run_id}': {last_error}")
        return False

    def load(self, run_id: str) -> Optional[dict[str, Any]]:
        """Load the last checkpoint for ``run_id``.

        Returns:
            The saved state dict, or ``None`` when there is no checkpoint, the
            file is missing/unreadable, or the JSON is corrupt. Any failure is
            logged and treated as a fresh start.
        """
        try:
            path = self._path(run_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[checkpoint] unreadable run '{run_id}': {exc}")
            return None
        try:
            state = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[checkpoint] corrupt run '{run_id}': {exc}")
            return None
        if not isinstance(state, dict):
            print(f"[checkpoint] invalid payload for run '{run_id}': not a dict")
            return None
        return state
