"""
FeedbackRanker — aplica bonus/penalty de feedback ao combined_score dos resultados.

Usa os scores acumulados do FeedbackStore para ajustar o ranking de resultados
já sintetizados. O ajuste é bounded para não inverter completamente o ranking:
delta máximo = ±15 pontos sobre o combined_score original.
"""

import logging
from typing import List, Optional
from src.types import SynthesizedResult
from src.feedback_store import FeedbackStore

logger = logging.getLogger(__name__)

_DELTA_CAP = 15.0   # máximo ajuste (positivo ou negativo) em pontos de score
_SCALE = 5.0        # cada unidade de feedback score = 5 pontos de combined_score


def _result_id(result: SynthesizedResult) -> str:
    """Gera um id estável a partir de entity + title (sem deps externas)."""
    raw = f"{result.entity}:{result.title}".lower().strip()
    import hashlib
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


class FeedbackRanker:
    def __init__(self, store: Optional[FeedbackStore] = None):
        self.store = store or FeedbackStore()

    def apply(self, results: List[SynthesizedResult]) -> List[SynthesizedResult]:
        """
        Ajusta combined_score de cada resultado com base no feedback acumulado.
        Reordena a lista por score ajustado (desc). Não modifica o objeto original
        — retorna cópias com score atualizado.
        """
        if not results:
            return results

        feedback_scores = self.store.get_scores()
        if not feedback_scores:
            return results

        adjusted = []
        for r in results:
            rid = _result_id(r)
            fb_score = feedback_scores.get(rid, 0.0)
            if fb_score == 0.0:
                adjusted.append(r)
                continue

            delta = max(-_DELTA_CAP, min(_DELTA_CAP, fb_score * _SCALE))
            new_score = round(max(0.0, min(100.0, r.combined_score + delta)), 2)

            if new_score != r.combined_score:
                logger.debug(f"FeedbackRanker: '{r.title[:40]}' {r.combined_score} → {new_score} (delta={delta:+.1f})")

            from dataclasses import replace
            adjusted.append(replace(r, combined_score=new_score))

        adjusted.sort(key=lambda x: x.combined_score, reverse=True)
        return adjusted

    def result_id_for(self, result: SynthesizedResult) -> str:
        """Expõe o id de um resultado para uso na tool MCP record_feedback."""
        return _result_id(result)
