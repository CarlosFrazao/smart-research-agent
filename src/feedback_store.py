"""
FeedbackStore — persiste sinais de feedback em JSONL para o FeedbackRanker.

Cada registro é uma linha JSON com: result_id, signal, timestamp, query.
O arquivo padrão é reports/_feedback.jsonl, configurável via FEEDBACK_STORE_PATH.
"""

import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

VALID_SIGNALS = {"useful", "not_useful", "bookmark", "irrelevant", "outdated"}

_DEFAULT_PATH = Path(__file__).parent.parent / "reports" / "_feedback.jsonl"


class FeedbackStore:
    def __init__(self, store_path: Optional[str] = None):
        self.path = Path(store_path or os.environ.get("FEEDBACK_STORE_PATH", str(_DEFAULT_PATH)))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, result_id: str, signal: str, query: str = "") -> dict:
        """Grava um sinal de feedback. Retorna o registro persistido."""
        if not result_id:
            raise ValueError("result_id não pode ser vazio")
        if signal not in VALID_SIGNALS:
            raise ValueError(f"signal inválido: '{signal}'. Válidos: {sorted(VALID_SIGNALS)}")

        entry = {
            "result_id": result_id,
            "signal": signal,
            "query": query,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info(f"Feedback gravado: {result_id} → {signal}")
        return entry

    def load_all(self) -> List[dict]:
        """Carrega todos os registros do arquivo JSONL."""
        if not self.path.exists():
            return []
        records = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(f"Linha inválida ignorada no feedback store: {line[:80]}")
        return records

    def get_scores(self) -> dict[str, float]:
        """
        Calcula o score acumulado por result_id.

        Sinais positivos (+): useful (+1.5), bookmark (+2.0)
        Sinais negativos (-): not_useful (-1.0), irrelevant (-1.5), outdated (-0.5)
        """
        weights = {
            "useful": 1.5,
            "bookmark": 2.0,
            "not_useful": -1.0,
            "irrelevant": -1.5,
            "outdated": -0.5,
        }
        scores: dict[str, float] = {}
        for rec in self.load_all():
            rid = rec.get("result_id", "")
            sig = rec.get("signal", "")
            if rid and sig in weights:
                scores[rid] = scores.get(rid, 0.0) + weights[sig]
        return scores

    def clear(self) -> int:
        """Remove todos os registros. Retorna quantidade deletada."""
        records = self.load_all()
        if self.path.exists():
            self.path.unlink()
        return len(records)
