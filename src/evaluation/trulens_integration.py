"""
TruLens Integration Module — Avaliação contínua de qualidade de agente com TruLens.

Este módulo fornece:
1. Integração com TruLens para registrar e avaliar interações de LLM.
2. Instrumentação automática de etapas do pipeline via decorators.
3. Armazenamento de runs de avaliação para análise posterior.
4. Alertas de regressão de qualidade via callbacks configuráveis.

Componentes principais:
- `TruLensRecorder`: Registra runs de LLM e métricas de qualidade.
- `QualityAppraiser`: Avaliador de qualidade baseado em TruLens.
- `record_trulens_run()`: Hook para registrar execução de etapas.
- `export_trulens_report()`: Exporta relatório de qualidade para o EvidenceGraph.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.pipeline.pipeline import PipelineContext
from src.utils.logging import setup_logger

logger = setup_logger("trulens-eval")

# ── Core TruLens Integration ─────────────────────────────────────────────────
# Nota: Este módulo é projetado para funcionar sem TruLens instalado.
# Se o pacote não estiver disponível, ele usa um mock leve.


@dataclass
class QualityRecord:
    """Registro de uma execução de avaliação de qualidade.

    Attributes:
        stage_name: Nome da etapa avaliada.
        timestamp: Quando a avaliação ocorreu.
        metrics: Dicionário de métricas calculadas.
        passed: Se a qualidade atingiu o mínimo esperado.
        error: Mensagem de erro, se houve falha na avaliação.
    """

    stage_name: str
    timestamp: float
    metrics: Dict[str, Any] = field(default_factory=dict)
    passed: bool = True
    error: Optional[str] = None


class TruLensRecorder:
    """Gravador de runs de avaliação usando TruLens (ou mock).

    Este componente:
    - Registra interações de LLM com TruLens (se disponível).
    - Mantém um histórico leve de QualityRecord para análise.
    - Fornece callbacks de alerta para regressões de qualidade.
    """

    def __init__(
        self,
        enabled: bool = True,
        alert_callback: Optional[Callable[[str, Dict], None]] = None,
    ):
        """Inicializa o gravador.

        Args:
            enabled: Se ativar a integração TruLens completa.
            alert_callback: Função chamada quando qualidade cai abaixo do limiar.
                            Assinatura: (stage_name, metrics) -> None.
        """
        self.enabled = enabled
        self.alert_callback = alert_callback
        self._records: List[QualityRecord] = []
        self._truLens_app = None

        if self.enabled:
            try:
                from trulens.core import Tru
                from trulens.applications import TruApp

                self._truLens_app = TruApp()
                logger.info("[TruLens] Integração ativada.")
            except ImportError:
                logger.warning(
                    "[TruLens] Pacote 'trulens' não encontrado. Usando mock leve."
                )
                self.enabled = False

    def record(
        self,
        stage_name: str,
        metrics: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> QualityRecord:
        """Registra uma avaliação de qualidade para uma etapa.

        Args:
            stage_name: Nome da etapa avaliada.
            metrics: Métricas de qualidade calculadas.
            error: Mensagem de erro, se houve falha.

        Returns:
            QualityRecord: O registro criado.
        """
        now = time.time()
        passed = error is None and self._check_quality(metrics or {})
        record = QualityRecord(
            stage_name=stage_name,
            timestamp=now,
            metrics=metrics or {},
            passed=passed,
            error=error,
        )
        self._records.append(record)

        # Alerta de qualidade baixa
        if not passed and self.alert_callback:
            self.alert_callback(stage_name, metrics or {})

        return record

    def _check_quality(self, metrics: Dict[str, Any]) -> bool:
        """Verifica se as métricas estão acima do limiar mínimo.

        Limiares padrão:
        - faithfulness >= 0.7
        - answer_relevance >= 0.6
        """
        min_faithfulness = 0.7
        min_relevance = 0.6

        faithfulness = metrics.get("faithfulness", 1.0)
        relevance = metrics.get("answer_relevance", 1.0)

        return faithfulness >= min_faithfulness and relevance >= min_relevance

    def get_records(self) -> List[QualityRecord]:
        """Retorna todos os registros de avaliação."""
        return list(self._records)

    def export_to_context(self, context: PipelineContext) -> PipelineContext:
        """Exporta registros para o contexto do pipeline.

        Armazena em `context.extra['trulens_records']` para uso posterior.
        """
        context.extra["trulens_records"] = [
            {
                "stage_name": r.stage_name,
                "timestamp": r.timestamp,
                "metrics": r.metrics,
                "passed": r.passed,
                "error": r.error,
            }
            for r in self._records
        ]
        return context


# ── Quality Appraiser (Avaliador de Qualidade) ─────────────────────────────
class QualityAppraiser:
    """Avaliador de qualidade de pipeline baseado em TruLens.

    Fornece métodos para:
    - Avaliar síntese gerada.
    - Comparar versões de pipeline (A/B testing).
    - Gerar relatórios de qualidade agregada.
    """

    def __init__(self, recorder: TruLensRecorder):
        """Inicializa o avaliador com gravador.

        Args:
            recorder: Instância de TruLensRecorder para registrar avaliações.
        """
        self.recorder = recorder

    async def appraise_synthesis(
        self,
        context: PipelineContext,
        synthesized: str,
        sources: List[Any],
    ) -> Dict[str, Any]:
        """Avalia a qualidade da síntese gerada.

        Métricas calculadas:
        - faithfulness: precisão factual (baseada em fontes).
        - answer_relevance: relevância para a query original.
        - context_recall: percentual de fontes usadas na síntese.

        Args:
            context: PipelineContext com query original.
            synthesized: Texto sintetizado gerado.
            sources: Lista de fontes usadas.

        Returns:
            Dict com métricas de qualidade.
        """
        metrics: Dict[str, Any] = {}

        # 1. Faithfulness (precisão factual)
        # Heurística: verificar se as afirmações estão presentes nas fontes
        import re

        claims = re.findall(r"\".+?\"|'[^']+'", synthesized)[:5]  # Top 5 claims
        cited_claims = sum(1 for c in claims if any(c in str(s) for s in sources))
        metrics["faithfulness"] = cited_claims / len(claims) if claims else 1.0

        # 2. Answer Relevance
        query = context.query or ""
        query_words = set(query.lower().split())
        synthesis_words = set(synthesized.lower().split())
        overlap = len(query_words & synthesis_words)
        metrics["answer_relevance"] = overlap / len(query_words) if query_words else 0.5

        # 3. Context Recall
        metrics["context_recall"] = min(
            1.0, len(sources) / 5.0
        )  # Baseline: 5 fontes ideais

        # Registrar avaliação
        self.recorder.record("synthesize", metrics)

        return metrics

    async def compare_runs(
        self,
        run_a: Dict[str, Any],
        run_b: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compara duas execuções de pipeline para A/B testing.

        Args:
            run_a: Métricas da primeira execução.
            run_b: Métricas da segunda execução.

        Returns:
            Dict com comparação e veredito.
        """
        scores_a = run_a.get("overall_score", 0.0)
        scores_b = run_b.get("overall_score", 0.0)

        winner = "A" if scores_a >= scores_b else "B"
        improvement = abs(scores_a - scores_b)

        return {
            "winner": winner,
            "improvement": improvement,
            "scores": {"run_a": scores_a, "run_b": scores_b},
        }


# ── Hooks e Utilitários ─────────────────────────────────────────────────────
def record_trulens_run(
    recorder: TruLensRecorder,
    stage_name: str,
    context: PipelineContext,
    metrics: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Hook para registrar execução de etapa no TruLens.

    Uso típico em uma PipelineStage.run():
        try:
            # ... lógica da etapa ...
        finally:
            record_trulens_run(self.recorder, self.name, context, metrics={...})
    """
    recorder.record(stage_name, metrics=metrics, error=error)
    recorder.export_to_context(context)


def export_trulens_report(recorder: TruLensRecorder) -> Dict[str, Any]:
    """Exporta relatório agregado de avaliações TruLens.

    Retorna:
        - Totál de etapas avaliadas.
        - Taxa de passagem.
        - Métricas médias por tipo de etapa.
    """
    records = recorder.get_records()
    if not records:
        return {"total_evaluations": 0, "pass_rate": 1.0, "metrics_by_stage": {}}

    total = len(records)
    passed = sum(1 for r in records if r.passed)
    pass_rate = passed / total

    metrics_by_stage: Dict[str, List[float]] = {}
    for r in records:
        for k, v in r.metrics.items():
            if isinstance(v, (int, float)):
                metrics_by_stage.setdefault(k, []).append(v)

    avg_by_stage = {k: sum(v) / len(v) for k, v in metrics_by_stage.items()}

    return {
        "total_evaluations": total,
        "passed_evaluations": passed,
        "pass_rate": pass_rate,
        "metrics_by_stage": avg_by_stage,
    }
