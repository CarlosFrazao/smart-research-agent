"""
Quality Gate Stage (Bloco 6 / E1-T2) — Guardiã automática de qualidade pós-síntese.

Estágio plugável do pipeline que avalia a resposta sintetizada via
``QualityGate`` (``src/quality_gate.py``) e registra o resultado em
``context.extra`` para observabilidade, auditoria e (futuramente) gap-fill.

Design:
- **Não-bloqueante.** Se o gate falhar nos thresholds, o pipeline prossegue;
  apenas sinalizamos ``quality_gate_failed=True`` e os scores em ``extra``.
- **Gracioso.** Falhas de métricas/timeouts nunca abortam o pipeline.
- **Determinístico hoje.** Usa o ``SynthesizedClaim`` (Bloco 5) como proxy de
  faithfulness quando o RAGAS real (langchain+ragas) não está instalado, e
  prefere métricas reais automaticamente quando disponíveis.
- Seguiu o mesmo padrão de DI e `best-effort` da ``VerificationStage``.

Posição no pipeline: após ``synthesize`` (claims já disponíveis) e antes de
``report`` (para que o relatório possa, no futuro, expor o score RAGAS).
"""

from __future__ import annotations

import logging
from typing import Any

from src.pipeline.pipeline import PipelineContext, PipelineStage
from src.quality_gate import QualityGate, QualityGateResult

logger = logging.getLogger("pipeline.quality_gate_stage")


class QualityGateStage(PipelineStage):
    """Estágio de Quality Gate RAGAS pós-síntese.

    Args:
        gate: Instância de ``QualityGate`` (injetada; construída lazy se None).
        enabled: Se o gate deve rodar. Quando False, o estágio é no-op.
        config: Objeto ``Config`` do SRA (lê thresholds/enable). Injetado para
            testes; quando None, usa os defaults do ``QualityGate``.
    """

    name = "quality_gate"

    def __init__(
        self,
        gate: QualityGate | None = None,
        *,
        enabled: bool = True,
        config: Any | None = None,
    ) -> None:
        self._gate = gate
        self.enabled = enabled
        self.config = config

    def _build_gate(self) -> QualityGate:
        """Constrói o ``QualityGate`` a partir da config ou defaults."""
        if self._gate is not None:
            return self._gate
        thresholds = {}
        if self.config is not None:
            thresholds = {
                "threshold_faithfulness": getattr(
                    self.config, "quality_gate_faithfulness_threshold", 0.70
                ),
                "threshold_relevancy": getattr(
                    self.config, "quality_gate_relevancy_threshold", 0.75
                ),
            }
        self._gate = QualityGate(**thresholds)
        return self._gate

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Avalia a qualidade da resposta e registra o resultado no contexto."""
        if not self.enabled:
            logger.info("QualityGateStage: desativado (enabled=False). Pulando.")
            context.extra["quality_gate_failed"] = False
            context.extra["quality_gate_result"] = None
            return context

        # Claims disponíveis (Bloco 5) — fonte primária do proxy determinístico.
        claims = list(context.extra.get("synthesized_claims") or [])
        # Fallback: deriva claims do Synthesizer se ainda não estiverem em extra.
        if not claims:
            claims = await self._derive_claims(context)

        # Contextos = snippets das fontes ranqueadas (para RAGAS real).
        contexts = self._build_contexts(context)

        logger.info(
            "QualityGateStage: avaliando %d claim(s) para query '%s'.",
            len(claims),
            context.query[:60],
        )

        try:
            gate = self._build_gate()
            result: QualityGateResult = await gate.evaluate(
                query=context.query,
                claims=claims,
                contexts=contexts,
            )
        except Exception as exc:  # pragma: no cover - defensivo
            logger.warning("QualityGateStage: erro não-fatal: %s", exc)
            context.extra["quality_gate_failed"] = False
            context.extra["quality_gate_result"] = None
            return context

        context.extra["quality_gate_result"] = result
        context.extra["quality_gate_failed"] = not result.passed

        if not result.passed:
            logger.warning(
                "quality_gate_failed faithfulness=%.3f relevancy=%.3f mode=%s query=%s",
                result.faithfulness,
                result.relevancy,
                result.mode,
                context.query[:60],
            )
        else:
            logger.info(
                "quality_gate_passed faithfulness=%.3f relevancy=%.3f mode=%s",
                result.faithfulness,
                result.relevancy,
                result.mode,
            )

        return context

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    async def _derive_claims(context: PipelineContext) -> list[Any]:
        """Deriva claims sob demanda dos ``synthesized_results`` do contexto.

        Usa ``Synthesizer.synthesize_with_claims`` (Bloco 5) quando disponível,
        sem quebrar se o método não existir.
        """
        synthesized = list(context.synthesized_results or [])
        if not synthesized:
            return []
        try:
            from src.synthesizer import Synthesizer

            _, claims = await Synthesizer().synthesize_with_claims(synthesized)
            return list(claims)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.debug("QualityGateStage: falha ao derivar claims: %s", exc)
            return []

    @staticmethod
    def _build_contexts(context: PipelineContext) -> list[str]:
        """Extrai snippets de contexto das fontes ranqueadas para o RAGAS real."""
        results = list(context.ranked_results or [])
        contexts: list[str] = []
        for r in results:
            title = getattr(r, "title", "") or ""
            desc = getattr(r, "description", "") or ""
            snippet = (f"{title}\n{desc}").strip()
            if snippet:
                contexts.append(snippet)
        return contexts
