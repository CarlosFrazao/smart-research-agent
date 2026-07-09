"""
FeedbackStore — persiste sinais de feedback em JSONL para o FeedbackRanker.

Cada registro é uma linha JSON com: result_id, signal, timestamp, query.
O arquivo padrão é reports/_feedback.jsonl, configurável via FEEDBACK_STORE_PATH.
"""

import json
import logging
import os
from datetime import datetime, UTC
from pathlib import Path

logger = logging.getLogger(__name__)

VALID_SIGNALS = {"useful", "not_useful", "bookmark", "irrelevant", "outdated"}

_DEFAULT_PATH = Path(__file__).parent.parent / "reports" / "_feedback.jsonl"

# Limites e parâmetros do rastreio de feedback por fonte (Fase 4).
_SOURCE_FEEDBACK_NEUTRAL_WEIGHT = 1.0  # sem histórico → peso neutro
_SOURCE_FEEDBACK_MIN_WEIGHT = 0.2  # nenhuma fonte é descartada totalmente
_SOURCE_FEEDBACK_MAX_WEIGHT = 2.0  # nenhuma fonte domina completamente
_SOURCE_FEEDBACK_MIN_VOLUME = 5  # volume mínimo antes de ajustar o peso


class FeedbackStore:
    def __init__(self, store_path: str | None = None):
        self.path = Path(
            store_path or os.environ.get("FEEDBACK_STORE_PATH", str(_DEFAULT_PATH))
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Arquivo separado para o feedback por fonte (mesma infraestrutura JSONL).
        self.source_path = self.path.parent / "_feedback_sources.jsonl"
        self.source_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, result_id: str, signal: str, query: str = "") -> dict:
        """Grava um sinal de feedback. Retorna o registro persistido."""
        if not result_id:
            raise ValueError("result_id não pode ser vazio")
        if signal not in VALID_SIGNALS:
            raise ValueError(
                f"signal inválido: '{signal}'. Válidos: {sorted(VALID_SIGNALS)}"
            )

        entry = {
            "result_id": result_id,
            "signal": signal,
            "query": query,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info(f"Feedback gravado: {result_id} → {signal}")
        return entry

    def load_all(self) -> list[dict]:
        """Carrega todos os registros do arquivo JSONL."""
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(
                        f"Linha inválida ignorada no feedback store: {line[:80]}"
                    )
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

    # ── Feedback por fonte (Fase 4 — personalização por usuário) ────────────────

    def record_source_feedback(
        self,
        user_id: str,
        source_name: str,
        query_domain: str,
        was_useful: bool,
        result_score: float = 0.0,
    ) -> None:
        """Registra qual fonte gerou resultado aproveitado/ignorado pelo usuário.

        Persiste em um arquivo JSONL separado (``self.source_path``), com o schema
        ``{user_id, source_name, domain, was_useful, score, timestamp}``.
        """
        if not user_id:
            raise ValueError("user_id não pode ser vazio")
        if not source_name:
            raise ValueError("source_name não pode ser vazio")
        # Domínio vazio cai no agregador neutro "general" (evita fragmentação de histórico).
        domain = query_domain or "general"

        entry = {
            "user_id": user_id,
            "source_name": source_name,
            "domain": domain,
            "was_useful": bool(was_useful),
            "score": float(result_score),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self.source_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.source_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info(
            f"Source feedback gravado: user={user_id} source={source_name} "
            f"domain={domain} useful={was_useful}"
        )

    def get_source_weights(
        self,
        user_id: str,
        domain: str,
        available_sources: list[str],
    ) -> dict[str, float]:
        """Retorna pesos de confiança por fonte para um usuário/domínio.

        Retorna ``dict {source_name: weight}`` onde ``weight ∈ [0.2, 2.0]``.
        ``weight > 1.0`` = fonte historicamente útil para este usuário.
        ``weight < 1.0`` = fonte historicamente ignorada.
        ``weight = 1.0`` = sem histórico (neutro).

        Regras:
        - Cold start (sem histórico) → todas as fontes retornam 1.0.
        - Só ajusta pesos após ≥5 feedbacks por combinação (usuário, domínio, fonte).
        - Fórmula: ``weight = 1.0 + (useful_ratio - 0.5) * 2``.
        - Peso limitado a ``[0.2, 2.0]``.
        """
        neutral = {src: _SOURCE_FEEDBACK_NEUTRAL_WEIGHT for src in available_sources}
        if not user_id or not available_sources:
            return neutral

        # Filtra pelo usuário e domínio solicitados.
        target_domain = domain or "general"
        records = [
            r
            for r in self._load_source_feedback()
            if r.get("user_id") == user_id and r.get("domain") == target_domain
        ]

        # Agrupa os sinais ``was_useful`` por fonte.
        per_source: dict[str, list[bool]] = {}
        for r in records:
            src = r.get("source_name", "")
            if src:
                per_source.setdefault(src, []).append(bool(r.get("was_useful", False)))

        weights = dict(neutral)
        for src in available_sources:
            feedbacks = per_source.get(src, [])
            if len(feedbacks) < _SOURCE_FEEDBACK_MIN_VOLUME:
                continue  # volume insuficiente → mantém peso neutro (anti-overfitting)
            approved = sum(1 for useful in feedbacks if useful)
            useful_ratio = approved / len(feedbacks)
            weight = _SOURCE_FEEDBACK_NEUTRAL_WEIGHT + (useful_ratio - 0.5) * 2.0
            weights[src] = max(
                _SOURCE_FEEDBACK_MIN_WEIGHT,
                min(_SOURCE_FEEDBACK_MAX_WEIGHT, weight),
            )
        return weights

    def clear_source_feedback(self, user_id: str | None = None) -> int:
        """Remove feedbacks por fonte.

        Se ``user_id`` for informado, limpa apenas o histórico daquele usuário;
        se ``None``, limpa todo o arquivo de feedback por fonte.

        Retorna a quantidade de registros removidos.
        """
        if not self.source_path.exists():
            return 0

        if user_id is None:
            removed = sum(1 for _ in self._load_source_feedback())
            self.source_path.unlink()
            return removed

        kept, removed = [], 0
        for rec in self._load_source_feedback():
            if rec.get("user_id") == user_id:
                removed += 1
            else:
                kept.append(rec)
        with open(self.source_path, "w", encoding="utf-8") as f:
            for rec in kept:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return removed

    def _load_source_feedback(self) -> list[dict]:
        """Carrega todos os registros de feedback por fonte do arquivo JSONL."""
        if not self.source_path.exists():
            return []
        records = []
        with open(self.source_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(
                        f"Linha inválida ignorada no source feedback store: {line[:80]}"
                    )
        return records
