"""
DynamicDecisionEngine — Implementação do motor de tomada de decisão dinâmica
para orquestração baseada em ReAct com feedback de avaliação contínua.

Este componente:
1. Evalua o contexto atual (confiança, lacunas, claims pendentes, métricas)
2. Decide qual etapa deve executar em seguida
3. Mantém cache de estados críticos para deliberação reversível
4. Integra métricas de avaliação (RAGAS/TruLens) para tomada de decisão informada
5. Fornece interface para o ReActOrchestrator usar

Regras de decisão (iniciais, simplificadas e explicáveis):
- Seult ainda não tem intent: deve executar 'intent'
- Se há intenção mas não há plano de fontes: deve executar 'expand' (ou 'storm')
- Se há plano mas não há resultados: deve executar 'search'
- Se há resultados mas não estão ranqueados: deve executar 'rank'
- Se há claims pendentes de verificação: deve executar 'verification'
- Se a confiança agregada < limiar: deve executar 'gap' (gap-fill)
- Se há gaps mas não novas queries: deve executar 'expand'
- Se tudo estiver completo: deve executar 'synthesize' -> 'report'
- Se o modo de operação é "guerrilha": pula etapas não-críticas (audit, graph_explorer)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from src.pipeline.pipeline import PipelineContext

logger = logging.getLogger("decision-engine")


@dataclass
class Decision:
    """Decisão do motor sobre próxima ação no loop ReAct.

    Attributes:
        next_stage: Nome da próxima etapa a executar (ou None para finalizar).
        reason: Explicação legível da decisão (para logs/auditoria).
        confidence: Confiança na decisão (0-1), usada para debugging.
        metadata: Dados extras da decisão (ex: flag de "skipping").
    """

    next_stage: Optional[str]
    reason: str
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


class DynamicDecisionEngine:
    """Motor de decisão dinâmica para seleção de estágios no loop ReAct.

    Mantém estado interno de deliberação (cache de decisões anteriores) para
    permitir reversibilidade e auditoria. Não modifica o contexto diretamente;
    apenas recomenda a próxima ação com base no estado observável.
    """

    def __init__(
        self,
        config: Any = None,
        *,
        confidence_threshold: float = 50.0,
        max_iterations: int = 10,
        operation_mode: str = "cirurgia",
    ) -> None:
        """Inicializa o motor com parâmetros de decisão.

        Args:
            config: Instância de Config (opcional, para ler flags dinâmicos).
            confidence_threshold: Limiar de confiança agregada (0-100).
            max_iterations: Máximo de iterações antes de forçar finalização.
            operation_mode: Modo de operação atual ("cirurgia" | "guerrilha").
        """
        self.config = config
        self.confidence_threshold = confidence_threshold
        self.max_iterations = max_iterations
        self.operation_mode = operation_mode

        # Cache de deliberação: lista de decisões tomadas (para auditoria)
        self._decision_history: list[Decision] = []
        # Contador de iterações no loop atual
        self._iteration: int = 0
        # Estágios já executados neste loop (para evitar repetição desnecessária)
        self._executed_stages: set[str] = set()

    def reset(self) -> None:
        """Reseta o estado do motor para uma nova execução de pesquisa."""
        self._decision_history.clear()
        self._iteration = 0
        self._executed_stages.clear()
        logger.debug("DynamicDecisionEngine: estado resetado.")

    def _aggregate_confidence(self, context: PipelineContext) -> float:
        """Calcula a confiança agregada atual a partir do contexto.

        Usa múltiplas fontes: scores de resultados ranqueados, métricas de
        avaliação contínua (RAGAS/TruLens), e claims verificadas.

        Returns:
            float: Confiança agregada em escala 0-100.
        """
        # 1. Confiança média dos resultados ranqueados
        ranked = context.ranked_results or []
        if ranked:
            scores = [getattr(r, "score", 0.0) or 0.0 for r in ranked]
            base_conf = sum(scores) / len(scores) if scores else 0.0
        else:
            base_conf = 0.0

        # 2. Boost de claims verificadas (VerificationStage)
        verified = context.extra.get("verified_claims", [])
        if verified:
            verified_ok = sum(1 for c in verified if c.get("status") == "verified")
            boost = min(20.0, (verified_ok / len(verified)) * 20.0)
            base_conf = min(100.0, base_conf + boost)

        # 3. Penalização por métricas de avaliação contínua ruins
        eval_metrics = context.extra.get("ragas_metrics", {})
        if eval_metrics:
            # Ex: answer_relevance < 0.5 reduz confiança
            for stage_metrics in eval_metrics.values():
                if isinstance(stage_metrics, dict):
                    for metric_name, value in stage_metrics.items():
                        if "relevance" in metric_name and isinstance(
                            value, (int, float)
                        ):
                            if value < 0.5:
                                base_conf = max(0.0, base_conf - 10.0)

        return max(0.0, min(100.0, base_conf))

    def _has_pending_claims(self, context: PipelineContext) -> bool:
        """Verifica se há claims de código pendentes de verificação."""
        raw = context.raw_results or []
        # Heurística simples: procura por blocos de código em descrições
        import re

        code_pattern = re.compile(r"```(?:python|js|ts|java|go|rust)?\s", re.IGNORECASE)
        for r in raw[:5]:  # Só checa os 5 primeiros para performance
            desc = getattr(r, "description", "")
            if desc is None:
                desc = ""
            elif not isinstance(desc, str):
                desc = str(desc) if desc else ""
            if code_pattern.search(desc):
                return True
        return False

    def _is_complete(self, context: PipelineContext) -> bool:
        """Verifica se a pesquisa atingiu completude suficiente."""
        # Completude mínima: intent + expanded + search + rank + synthesize
        required = {"intent", "expand", "search", "rank", "synthesize"}
        has_report = context.report and len(context.report) > 0
        return required.issubset(self._executed_stages) and has_report

    def decide(self, context: PipelineContext) -> Decision:
        """Decide a próxima ação com base no estado atual do contexto.

        Args:
            context: PipelineContext com estado acumulado das etapas anteriores.

        Returns:
            Decision: Próxima ação recomendada (next_stage=None significa finalizar).
        """
        self._iteration += 1

        # Guard: iteração máxima atingida → forçar síntese+relatório
        if self._iteration > self.max_iterations:
            if "synthesize" not in self._executed_stages:
                return self._make_decision(
                    "synthesize", "Iteração máxima atingida; forçando síntese."
                )
            if "report" not in self._executed_stages:
                return self._make_decision(
                    "report", "Iteração máxima atingida; forçando relatório."
                )
            return self._make_decision(None, "Iteração máxima atingida; finalizando.")

        # 1. Sem intent → deve começar com intent
        if "intent" not in self._executed_stages:
            return self._make_decision("intent", "Intent ainda não executado.")

        # 2. Sem plano de fontes → expand (ou storm se enable_storm)
        if not context.source_plan and "expand" not in self._executed_stages:
            return self._make_decision(
                "expand", "Plano de fontes ausente; expandindo queries."
            )

        # 3. Sem resultados → search
        if not context.raw_results and "search" not in self._executed_stages:
            return self._make_decision(
                "search", "Resultados ausentes; executando busca."
            )

        # 4. Sem ranqueamento → rank
        if not context.ranked_results and "rank" not in self._executed_stages:
            return self._make_decision("rank", "Resultados não ranqueados; ranqueando.")

        # 5. Claims pendentes → verification (se não executado ainda)
        if (
            self._has_pending_claims(context)
            and "verification" not in self._executed_stages
            and "rank" in self._executed_stages
        ):
            return self._make_decision(
                "verification", "Claims de código detectadas; verificando em sandbox."
            )

        # 6. Confiança baixa → gap-fill (se não executado recentemente)
        confidence = self._aggregate_confidence(context)
        if (
            confidence < self.confidence_threshold
            and "gap" not in self._executed_stages
        ):
            return self._make_decision(
                "gap",
                f"Confiança agregada ({confidence:.1f}) abaixo do limiar "
                f"({self.confidence_threshold}); executando gap-fill.",
            )

        # 7. Gaps detectados mas sem novas queries → expand novamente
        gap_analysis = context.gap_analysis
        if gap_analysis and not getattr(gap_analysis, "is_complete", True):
            missing = getattr(gap_analysis, "missing_aspects", [])
            if (
                missing and "expand" not in self._executed_stages[-3:]
            ):  # Não repetir muito
                return self._make_decision(
                    "expand",
                    f"Lacunas detectadas ({len(missing)}); expandindo novamente.",
                )

        # 8. Modo guerrilha → pular etapas não-críticas
        if self.operation_mode == "guerrilha":
            if "synthesize" not in self._executed_stages:
                return self._make_decision(
                    "synthesize", "Modo guerrilha; síntese direta."
                )
            if "report" not in self._executed_stages:
                return self._make_decision(
                    "report", "Modo guerrilha; relatório direto."
                )
            return self._make_decision(None, "Modo guerrilha; pesquisa concluída.")

        # 9. Fluxo padrão: graph_explorer → synthesize → report
        if "graph_explorer" not in self._executed_stages and context.ranked_results:
            return self._make_decision(
                "graph_explorer", "Explorando grafo de conhecimento para insights."
            )

        if "synthesize" not in self._executed_stages:
            return self._make_decision("synthesize", "Sintetizando resultados.")

        if "report" not in self._executed_stages:
            return self._make_decision("report", "Gerando relatório final.")

        if "audit" not in self._executed_stages:
            return self._make_decision("audit", "Sanitizando e auditando relatório.")

        # 10. Completude → finalizar
        if self._is_complete(context):
            return self._make_decision(
                None, "Pesquisa completa; finalizando loop ReAct."
            )

        # Fallback: síntese+relatório para garantir saída
        if "synthesize" not in self._executed_stages:
            return self._make_decision("synthesize", "Fallback: síntese final.")
        if "report" not in self._executed_stages:
            return self._make_decision("report", "Fallback: relatório final.")
        return self._make_decision(None, "Fallback: finalizando.")

    def mark_executed(self, stage_name: str) -> None:
        """Registra que uma etapa foi executada (chamado pelo ReActOrchestrator)."""
        self._executed_stages.add(stage_name)
        logger.debug("DynamicDecisionEngine: '%s' marcado como executado.", stage_name)

    def _make_decision(self, next_stage: Optional[str], reason: str) -> Decision:
        """Cria e registra uma decisão no histórico."""
        decision = Decision(
            next_stage=next_stage,
            reason=reason,
            confidence=1.0,
            metadata={"iteration": self._iteration},
        )
        self._decision_history.append(decision)
        logger.info(
            "DynamicDecisionEngine: próxima etapa='%s' | %s",
            next_stage or "FINALIZAR",
            reason,
        )
        return decision

    def get_decision_history(self) -> list[Decision]:
        """Retorna o histórico de decisões para auditoria/debugging."""
        return list(self._decision_history)

    def export_decision_trace(self) -> dict[str, Any]:
        """Exporta trace de decisões para o EvidenceGraph/relatório final."""
        return {
            "iterations": self._iteration,
            "executed_stages": sorted(self._executed_stages),
            "decisions": [
                {
                    "iteration": d.metadata.get("iteration"),
                    "next_stage": d.next_stage,
                    "reason": d.reason,
                    "confidence": d.confidence,
                }
                for d in self._decision_history
            ],
        }
