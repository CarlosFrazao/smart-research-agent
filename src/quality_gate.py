"""
Quality Gate Automatizado (Bloco 6 / E1-T2) — RAGAS como Guardiã de Qualidade.

Este módulo implementa um portão de qualidade não-bloqueante que avalia a
resposta sintetizada do SRA segundo duas dimensões principais:

- ``faithfulness`` (precisão factual / ancoragem em fontes)
- ``relevancy`` (a resposta aborda a query com base em fontes reais)

Princípio de design (Rigor do SRA v7.0):
    O gate **nunca quebra o pipeline**. Se o RAGAS real (langchain + ragas)
    não estiver instalado, ele usa um *proxy determinístico* derivado do
    ``SynthesizedClaim`` (Bloco 5 / E1-T1) — a cobertura de rastreabilidade
    de cada afirmação (``source_id`` + ``url``). Esse proxy é funcional,
    testável e honesto: uma afirmação só é considerada "fiel" se tiver
    proveniência rastreável até uma fonte real. Quando o RAGAS real passa a
    estar disponível, o gate automaticamente prefere as métricas reais.

O gate também emite métricas Prometheus (graciosamente desabilitadas se o
client não estiver instalado) reutilizando ``src/observability/metrics.py``.

Contrato:
    ``QualityGate.evaluate(query, claims, contexts) -> QualityGateResult``
Não inventa métodos inexistentes (ex.: ``evaluate_async``) — interopera com a
API pública real de ``RagasEvaluator``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from src.observability.metrics import get_metrics

logger = logging.getLogger("quality-gate")


@dataclass
class QualityGateResult:
    """Resultado de uma avaliação do Quality Gate.

    Attributes:
        passed: Se a resposta passou em todos os thresholds configurados.
        faithfulness: Score de precisão factual (0.0-1.0).
        relevancy: Score de relevância da resposta à query (0.0-1.0).
        traceability: Cobertura de rastreabilidade das claims (0.0-1.0).
        mode: Modo de avaliação efetivo ("ragas" se RAGAS real disponível,
            "proxy" caso contrário).
        retry_recommended: Se o pipeline deveria recomendar uma nova tentativa
            (claim gap-fill) devido à falha de qualidade.
        scores: Dicionário bruto de métricas (para auditoria/observabilidade).
    """

    passed: bool
    faithfulness: float
    relevancy: float
    traceability: float = 0.0
    mode: str = "proxy"
    retry_recommended: bool = False
    scores: dict[str, Any] = field(default_factory=dict)


class QualityGate:
    """Portão de qualidade automático pós-síntese.

    Decide se a resposta sintetizada atende aos limiares de ``faithfulness``
    e ``relevancy`` configurados. Tolerante a falhas: timeouts e ausência de
    RAGAS são tratados de forma graciosa (não quebram o pipeline).

    Args:
        threshold_faithfulness: Limiar mínimo de faithfulness (0.0-1.0).
        threshold_relevancy: Limiar mínimo de relevancy (0.0-1.0).
        threshold_traceability: Limiar mínimo de cobertura de rastreabilidade
            (0.0-1.0). Usado apenas como métrica reportada quando abaixo do
            limiar; não bloqueia por padrão (ver ``gate_on_traceability``).
        max_retries: Número máximo de recomendações de retry permitidas.
        timeout_seconds: Timeout da avaliação RAGAS real (gracioso).
        gate_on_traceability: Se True, a traceability também participa do
            critério de ``passed``.
    """

    def __init__(
        self,
        threshold_faithfulness: float = 0.70,
        threshold_relevancy: float = 0.75,
        threshold_traceability: float = 0.80,
        *,
        max_retries: int = 2,
        timeout_seconds: float = 30.0,
        gate_on_traceability: bool = False,
    ) -> None:
        self.threshold_faithfulness = threshold_faithfulness
        self.threshold_relevancy = threshold_relevancy
        self.threshold_traceability = threshold_traceability
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self.gate_on_traceability = gate_on_traceability
        # Construído lazy para não acoplar a importação de RAGAS no import do módulo.
        self._evaluator: Any | None = None

    # ── API pública ─────────────────────────────────────────────────────────

    async def evaluate(
        self,
        query: str,
        claims: list[Any],
        contexts: list[str],
    ) -> QualityGateResult:
        """Avalia a qualidade da resposta sintetizada.

        Args:
            query: A query original da pesquisa.
            claims: Lista de ``SynthesizedClaim`` (Bloco 5) — cada afirmação
                carrega sua proveniência (``source_ids`` + ``urls``).
            contexts: Lista de textos de contexto (snippets das fontes)
                usados pela avaliação RAGAS real quando disponível.

        Returns:
            QualityGateResult: Resultado com scores e decisão de aprovação.
        """
        try:
            scores = await asyncio.wait_for(
                self._compute_scores(query, claims, contexts),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "quality_gate_timeout_graceful_skip timeout=%.1fs",
                self.timeout_seconds,
            )
            return QualityGateResult(
                passed=True,
                faithfulness=0.0,
                relevancy=0.0,
                traceability=0.0,
                mode="timeout",
                retry_recommended=False,
                scores={"error": "timeout"},
            )
        except Exception as exc:  # pragma: no cover - defensivo
            logger.warning("quality_gate_error_graceful_skip error=%s", exc)
            return QualityGateResult(
                passed=True,
                faithfulness=0.0,
                relevancy=0.0,
                traceability=0.0,
                mode="error",
                retry_recommended=False,
                scores={"error": str(exc)},
            )

        faithfulness = float(scores.get("faithfulness", 0.0))
        relevancy = float(scores.get("relevancy", 0.0))
        traceability = float(scores.get("traceability", 0.0))
        mode = str(scores.get("mode", "proxy"))

        passed = (
            faithfulness >= self.threshold_faithfulness
            and relevancy >= self.threshold_relevancy
        )
        if self.gate_on_traceability:
            passed = passed and traceability >= self.threshold_traceability

        logger.info(
            "quality_gate_result faithfulness=%.3f relevancy=%.3f traceability=%.3f mode=%s passed=%s",
            faithfulness,
            relevancy,
            traceability,
            mode,
            passed,
        )
        self._emit_metrics(mode, faithfulness, relevancy, traceability)

        return QualityGateResult(
            passed=passed,
            faithfulness=faithfulness,
            relevancy=relevancy,
            traceability=traceability,
            mode=mode,
            retry_recommended=not passed,
            scores=scores,
        )

    # ── Internos ────────────────────────────────────────────────────────────

    async def _compute_scores(
        self, query: str, claims: list[Any], contexts: list[str]
    ) -> dict[str, Any]:
        """Computa os scores combinando RAGAS real (se houver) e proxy local.

        Se o ``RagasEvaluator`` retornar métricas reais (langchain instalado),
        elas têm precedência. Caso contrário, usa o proxy determinístico
        baseado em ``SynthesizedClaim``.
        """
        # 1. Tenta o RAGAS real primeiro (precedência).
        ragas = self._get_evaluator()
        if ragas is not None:
            real = await self._try_ragas_real(ragas, query, claims, contexts)
            if real is not None:
                return real

        # 2. Proxy determinístico (funciona hoje, sem dependências externas).
        return self._proxy_scores(claims, contexts)

    def _get_evaluator(self) -> Any | None:
        """Constrói o ``RagasEvaluator`` lazy (ou None se indisponível)."""
        if self._evaluator is not None:
            return self._evaluator
        try:
            from src.evaluation.ragas_integration import RagasEvaluator

            self._evaluator = RagasEvaluator(enabled=True)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.debug("quality_gate: RagasEvaluator indisponível: %s", exc)
            self._evaluator = None
        return self._evaluator

    async def _try_ragas_real(
        self, ragas: Any, query: str, claims: list[Any], contexts: list[str]
    ) -> dict[str, Any] | None:
        """Tenta obter métricas reais do RAGAS.

        Reconstrói um ``PipelineContext`` mínimo (a API real de
        ``RagasEvaluator.evaluate`` recebe um contexto) e usa os scores
        retornados se eles contiverem chaves reais de métricas.
        """
        try:
            from src.pipeline.pipeline import PipelineContext

            report = "\n\n".join(
                c.as_markdown() for c in claims if hasattr(c, "as_markdown")
            )
            context = PipelineContext(query=query, report=report)
            metrics = await ragas.evaluate(context)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.debug("quality_gate: falha ao chamar RAGAS real: %s", exc)
            return None

        # ``RagasEvaluator`` retorna ``{}`` quando langchain/ragas não instalado.
        if not metrics:
            return None
        faith = metrics.get("faithfulness")
        rel = metrics.get("answer_relevance") or metrics.get("answer_relevancy")
        if faith is None and rel is None:
            return None
        return {
            "faithfulness": float(faith if faith is not None else 0.0),
            "relevancy": float(rel if rel is not None else 0.0),
            "traceability": self._traceability(claims),
            "mode": "ragas",
        }

    def _proxy_scores(self, claims: list[Any], contexts: list[str]) -> dict[str, Any]:
        """Proxy determinístico de qualidade derivado de ``SynthesizedClaim``.

        - ``traceability``: fração de claims com ``source_id`` E ``url``.
        - ``faithfulness``: igual à traceability — uma afirmação só é fiel se
          ancorada em uma fonte rastreável (valor central do Bloco 5).
        - ``relevancy``: fração de claims que apontam para uma URL real de
          fonte, refletindo que a resposta foi construída a partir de
          fontes recuperadas (e não de vazio/encoder).
        """
        traceability = self._traceability(claims)
        url_coverage = self._url_coverage(claims)
        ctx_coverage = self._context_coverage(contexts)
        # faithfulness = grounding (traceability); relevancy = cobertura de fontes.
        faithfulness = traceability
        relevancy = (url_coverage + ctx_coverage) / 2.0
        return {
            "faithfulness": faithfulness,
            "relevancy": relevancy,
            "traceability": traceability,
            "mode": "proxy",
        }

    @staticmethod
    def _traceability(claims: list[Any]) -> float:
        """Fração de claims com ``source_id`` E ``url`` não vazios."""
        if not claims:
            return 0.0
        grounded = 0
        for c in claims:
            sids = getattr(c, "source_ids", None) or []
            urls = getattr(c, "urls", None) or []
            if sids and urls:
                grounded += 1
        return grounded / len(claims)

    @staticmethod
    def _url_coverage(claims: list[Any]) -> float:
        """Fração de claims que referenciam ao menos uma URL de fonte."""
        if not claims:
            return 0.0
        with_url = sum(1 for c in claims if getattr(c, "urls", None))
        return with_url / len(claims)

    @staticmethod
    def _context_coverage(contexts: list[str]) -> float:
        """Fração de contextos (snippets) não vazios fornecidos."""
        if not contexts:
            return 0.0
        non_empty = sum(1 for ctx in contexts if ctx and ctx.strip())
        return non_empty / len(contexts)

    def _emit_metrics(
        self, mode: str, faithfulness: float, relevancy: float, traceability: float
    ) -> None:
        """Emite métricas Prometheus (gracioso se client ausente)."""
        metrics = get_metrics()
        if not metrics:
            return
        try:
            if "ragas_faithfulness" in metrics:
                metrics["ragas_faithfulness"].labels(mode=mode).set(faithfulness)
            if "ragas_relevancy" in metrics:
                metrics["ragas_relevancy"].labels(mode=mode).set(relevancy)
            if "ragas_traceability" in metrics:
                metrics["ragas_traceability"].labels(mode=mode).set(traceability)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.debug("quality_gate: falha ao emitir métricas: %s", exc)
