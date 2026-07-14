"""Adversarial Pass Stage — Passada adversarial leve anti viés de confirmação.

FASE 3 do Plano Parte 4 (Linhagem & Adversarial).

Responsabilidade
-----------------
Gera UMA query deliberadamente formulada para desafiar a conclusão emergente
da pesquisa e executa uma rodada de busca extra com ela. Custo: 1 query + 1
rodada de busca. Os resultados são marcados com ``is_adversarial=True`` e
anexados a ``ranked_results`` como evidência de primeira classe (não uma seção
separada), alimentando a seção de confiança do relatório.

Ativada por ``OperationConfig.enable_adversarial_pass`` (True em modos
cirurgia/arqueologia/black_ops, False em guerrilha/padrao).

Design
------
Reusa o ``SearchStage`` real (via injeção de dependência) para disparar a
busca adversarial — assim aproveita semáforos, circuit breaker, timeouts e
ranking já existentes, em vez de duplicar essa lógica. Se o ``SearchStage``
não estiver disponível (uso isolado/testes), faz fallback para o searcher
direto quando possível, ou pula graciosamente.

A geração da query adversarial segue o contrato de prompt da skill
``prompt-engineering``: saída explícita (só a query), temperatura baixa,
comportamento de falha explícito e logging estruturado.
"""

from __future__ import annotations

import logging
from typing import Any

from src.pipeline.pipeline import PipelineContext, PipelineStage
from src.types import SearchResult

logger = logging.getLogger("pipeline.adversarial_stage")


class AdversarialPassStage(PipelineStage):
    """Gera uma query adversarial para combater viés de confirmação.

    Attributes:
        name: Identificador do stage no pipeline.
        critical: False — a passada adversarial é um enriquecimento; falhas
            nela nunca devem abortar a pesquisa principal.
    """

    name = "adversarial_pass"
    critical = False

    def __init__(
        self,
        llm_client: Any = None,
        search_stage: Any = None,
    ) -> None:
        """Inicializa o stage.

        Args:
            llm_client: Cliente LLM para gerar a query adversarial. Se None,
                a geração é pulada e o stage retorna o contexto inalterado.
            search_stage: Instância de ``SearchStage`` para reusar a lógica de
                busca real (semáforos, circuit breaker, ranking). Se None, o
                stage tenta buscar direto pelos searchers do orchestrator e,
                se impossível, pula a busca.
        """
        self._llm = llm_client
        self._search_stage = search_stage

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Executa a passada adversarial se o modo de operação a habilitar.

        Args:
            context: Contexto do pipeline (lê ``ranked_results``/modo, anexa
                resultados adversariais).

        Returns:
            PipelineContext: O mesmo contexto, com resultados adversariais
            anexados a ``ranked_results`` quando aplicável.
        """
        # 1. Respeita o gate do modo de operação.
        if not self._adversarial_enabled(context):
            logger.debug(
                "AdversarialPassStage: desabilitado para este modo de operação; pulando."
            )
            return context

        if self._llm is None:
            logger.warning(
                "AdversarialPassStage: sem LLM client; pulando passada adversarial."
            )
            return context

        # 2. Gera a query adversarial a partir da conclusão emergente.
        emerging_conclusion = self._summarize_top_results(
            context.ranked_results, context.query
        )
        try:
            adversarial_query = await self._generate_adversarial_query(
                original_query=context.query,
                emerging_conclusion=emerging_conclusion,
            )
        except Exception as exc:  # noqa: BLE001 - geração nunca deve quebrar o pipeline
            logger.warning(
                "AdversarialPassStage: falha ao gerar query adversarial: %s", exc
            )
            return context

        if not adversarial_query:
            logger.warning(
                "AdversarialPassStage: query adversarial vazia; pulando busca."
            )
            return context

        logger.info("AdversarialPassStage: query adversarial = '%s'", adversarial_query)

        # 3. Executa a busca adversarial reusando o SearchStage real.
        try:
            adversarial_results = await self._run_search(adversarial_query, context)
        except Exception as exc:  # noqa: BLE001 - busca nunca deve quebrar o pipeline
            logger.warning("AdversarialPassStage: falha na busca adversarial: %s", exc)
            return context

        if not adversarial_results:
            logger.info(
                "AdversarialPassStage: nenhum resultado adversarial encontrado."
            )
            return context

        # 4. Marca e injeta como evidência de primeira classe.
        for r in adversarial_results:
            self._safe_set(r, "is_adversarial", True)
        context.ranked_results.extend(adversarial_results)
        context.set("adversarial_query", adversarial_query)
        context.set("adversarial_hits", len(adversarial_results))

        logger.info(
            "AdversarialPassStage: %d resultado(s) adversarial(is) injetado(s).",
            len(adversarial_results),
        )
        return context

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _adversarial_enabled(context: PipelineContext) -> bool:
        """Lê ``enable_adversarial_pass`` do modo de operação via orchestrator."""
        orchestrator = context.extras.get("orchestrator") if context.extras else None
        op_mode = (
            getattr(orchestrator, "operation_mode", None) if orchestrator else None
        )
        return bool(getattr(op_mode, "enable_adversarial_pass", False))

    def _summarize_top_results(
        self, ranked_results: list[Any], original_query: str, top_n: int = 5
    ) -> str:
        """Monta um resumo sintético dos top-N resultados para embasar a query."""
        snippets: list[str] = []
        for r in ranked_results[:top_n]:
            title = (getattr(r, "title", "") or "").strip()
            desc = (getattr(r, "description", "") or "").strip()
            if title or desc:
                snippets.append(f"- {title}: {desc[:160]}")
        summary = "\n".join(snippets)
        return summary or original_query

    async def _generate_adversarial_query(
        self, original_query: str, emerging_conclusion: str
    ) -> str:
        """Gera UMA query que busca evidências CONTRA a conclusão emergente.

        Segue o contrato da skill prompt-engineering: saída única e explícita
        (só a query), temperatura baixa (0.3) e instrução de falha explícita.
        """
        prompt = (
            "Você é um cético rigoroso. Dada a query original e a conclusão "
            "emergente de uma pesquisa, formule UMA única query de busca que "
            "procure deliberadamente evidências CONTRA essa conclusão.\n\n"
            "Use padrões como: 'problemas com', 'críticas a', 'falhas de', "
            "'por que X está errado', 'riscos de', 'limitações de'.\n\n"
            f"Query original: {original_query}\n"
            f"Conclusão emergente: {emerging_conclusion}\n\n"
            "Responda SOMENTE com a query (sem aspas, sem explicação, sem "
            "texto adicional). Se não conseguir formular, responda 'N/A'."
        )
        response = await self._llm.complete(prompt, temperature=0.3, max_tokens=80)
        query = (response or "").strip().strip('"').strip("'")
        if not query or query.upper() == "N/A":
            return ""
        return query

    async def _run_search(
        self, adversarial_query: str, context: PipelineContext
    ) -> list[SearchResult]:
        """Executa a busca adversarial reusando o ``SearchStage`` real.

        Constrói um ``source_plan`` mínimo a partir dos searchers efetivamente
        consultados nesta pesquisa (preservando o modo de operação) e delega a
        busca/ranking ao ``SearchStage`` injetado. Se o ``SearchStage`` não
        estiver disponível, tenta busca direta pelos searchers do orchestrator.
        """
        search_stage = self._search_stage
        orchestrator = context.extras.get("orchestrator") if context.extras else None

        if search_stage is not None:
            searchers = getattr(search_stage, "searchers", None) or (
                getattr(orchestrator, "searchers", None) if orchestrator else None
            )
            if not searchers:
                logger.warning(
                    "AdversarialPassStage: sem searchers; pulando busca adversarial."
                )
                return []
            try:
                from src.types import ExpandedQuery, SourcePlan

                plan = SourcePlan(
                    sources={
                        name: [
                            ExpandedQuery(query=adversarial_query, type="adversarial")
                        ]
                        for name in searchers
                    }
                )
                # Reusa a lógica de busca real de forma isolada: construímos um
                # contexto efêmero para não poluir o contexto principal.
                from src.pipeline.pipeline import PipelineContext as _Ctx

                adv_ctx = _Ctx(query=adversarial_query)
                adv_ctx.source_plan = plan
                adv_ctx.intent = context.intent
                adv_ctx.metadata = {}
                adv_ctx.extras["orchestrator"] = orchestrator
                adv_ctx.extras["session_id"] = context.extras.get(
                    "session_id", "default_session"
                )
                await search_stage.run(adv_ctx)
                return list(adv_ctx.ranked_results or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("AdversarialPassStage: SearchStage falhou: %s", exc)
                return []

        # Fallback: busca direta pelos searchers se não houver SearchStage.
        searchers = getattr(orchestrator, "searchers", None) if orchestrator else None
        return await self._run_search_direct(adversarial_query, searchers)

    async def _run_search_direct(
        self, adversarial_query: str, searchers: dict[str, Any] | None
    ) -> list[SearchResult]:
        """Busca direta (fallback) sem SearchStage — sem ranking/circuit breaker."""
        if not searchers:
            return []
        results: list[SearchResult] = []
        import asyncio

        domain = ""

        async def _one(source_name: str, searcher: Any) -> None:
            try:
                found = await asyncio.wait_for(
                    searcher.search(adversarial_query, domain=domain),
                    timeout=20.0,
                )
                for r in found or []:
                    if isinstance(r, SearchResult):
                        results.append(r)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "AdversarialPassStage: searcher '%s' falhou: %s",
                    source_name,
                    exc,
                )

        await asyncio.gather(
            *[_one(name, sr) for name, sr in searchers.items()],
            return_exceptions=True,
        )
        return results

    @staticmethod
    def _safe_set(obj: Any, attr: str, value: Any) -> None:
        """Atribui ``value`` a ``obj.attr`` sem quebrar se o modelo rejeitar."""
        try:
            setattr(obj, attr, value)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "AdversarialPassStage: não foi possível setar %s=%r: %s",
                attr,
                value,
                exc,
            )


__all__ = ["AdversarialPassStage"]
