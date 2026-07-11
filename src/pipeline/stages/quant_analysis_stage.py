"""
quant_analysis_stage.py — Estágio de análise quantitativa (Fase 6.4).

Religa o ``DataAnalyzer`` (``src/data_analyzer.py``) ao pipeline. O
``DataAnalyzer`` gera e executa scripts Pandas em sandbox Docker isolada
(via ``CodeExecutionAgent``) sobre dados crus (CSVs/JSONs) extraídos das
fontes de pesquisa — realizando análise quantitativa real (market share,
benchmarks, agregações estatísticas) e não apenas verificação de claims.

O estágio é **não-crítico** e só executa quando há arquivos de dados
disponíveis em ``context.extra["data_files"]`` E uma pergunta analítica
em ``context.extra["data_question"]``. Se faltar algum, ele pula
graciosamente (best-effort), sem abortar o pipeline.

Entradas (``context.extra``):
  - ``data_files``: lista de caminhos ``str`` para CSVs/JSONs brutos.
  - ``data_question``: pergunta analítica (ex.: "qual o market share?").
  - ``data_timeout``: opcional, timeout em segundos para a sandbox.

Saída (``context.extra["quant_analysis"]``):
  ``DataAnalysisResult.to_dict()`` (question, script, stdout, stderr,
  exit_code, timed_out, status, error_message, files_analyzed).
"""

from __future__ import annotations

import logging
from typing import Any

from src.pipeline.pipeline import PipelineContext, PipelineStage

logger = logging.getLogger("quant-analysis-stage")


class QuantAnalysisStage(PipelineStage):
    """Estágio não-crítico de análise quantitativa via DataAnalyzer."""

    name = "quant_analysis"
    critical = False

    def __init__(
        self,
        data_analyzer: Any | None = None,
        llm_client: Any = None,
    ) -> None:
        """
        Args:
            data_analyzer: Instância de ``DataAnalyzer`` (lazy-built se None).
            llm_client: Cliente LLM opcional, repassado ao ``DataAnalyzer``
                para geração do script Pandas via LLM (fallback heurístico
                se None).
        """
        super().__init__()
        self._data_analyzer = data_analyzer
        self._llm = llm_client

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Executa a análise quantitativa se houver dados + pergunta."""
        data_files = context.extra.get("data_files") or []
        question = (context.extra.get("data_question") or "").strip()

        if not data_files or not question:
            logger.info(
                "QuantAnalysisStage: sem arquivos de dados ou pergunta. Pulando."
            )
            context.extra["quant_analysis"] = None
            return

        analyzer = self._data_analyzer or self._build_analyzer()
        if analyzer is None:
            logger.warning("QuantAnalysisStage: DataAnalyzer indisponível. Pulando.")
            context.extra["quant_analysis"] = None
            return

        timeout = context.extra.get("data_timeout")
        logger.info(
            "QuantAnalysisStage: analisando %d arquivo(s): %r",
            len(data_files),
            question[:60],
        )
        try:
            result = analyzer.analyze(
                data_paths=list(data_files),
                question=question,
                **({"timeout": float(timeout)} if timeout else {}),
            )
            context.extra["quant_analysis"] = result.to_dict()
            logger.info(
                "QuantAnalysisStage: concluído (status=%s).",
                result.status,
            )
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning("QuantAnalysisStage: falha na análise: %s", e)
            context.extra["quant_analysis"] = {
                "question": question,
                "status": "error",
                "error_message": str(e),
            }
        return context

    def _build_analyzer(self) -> Any | None:
        try:
            from src.data_analyzer import DataAnalyzer
            from src.services.code_execution_agent import CodeExecutionAgent

            return DataAnalyzer(code_agent=CodeExecutionAgent(), llm_client=self._llm)
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning("QuantAnalysisStage: falha ao criar DataAnalyzer: %s", e)
            return None
