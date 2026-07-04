"""
research_auditor.py — Loop de Auditoria Autônoma de Relatórios

Pipeline:
  1. Skip antecipado se as fontes já têm confiança média alta (economiza LLM+buscas)
  2. Extrai claims do relatório Markdown via LLM
  3. Valida claims contra fontes existentes (ConfidenceScorerV2)
  4. Detecta gaps: claims não verificadas ou de fonte única
  5. Relança buscas focadas nos gaps (padrão: 1 iteração — budget-enforced)
  6. Retorna relatório enriquecido com status de auditoria

Custo controlado por dois mecanismos independentes:
  - `max_iterations`: teto de rodadas de re-pesquisa (default: 1, era 3).
  - `audit_budget_usd`: teto de gasto estimado (USD) por chamada de `audit()`,
    reaproveitando `src.token_economy.Budget`. Ao esgotar, a auditoria para
    de re-pesquisar gaps e retorna o que já validou (degradação graciosa).

Skill: adversarial-debate-engine (Auto-crítica adversária sistemática)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.clients.llm_client import LLMClient
from src.token_economy import Budget, TokenEconomy, UsageRecord
from src.types import SearchResult

logger = logging.getLogger(__name__)

# Reduzido de 3 → 1: cada iteração adicional dispara novas buscas (caro) para
# um ganho marginal de cobertura. Modos que realmente precisam de mais podem
# passar `max_iterations` explicitamente ao chamar `audit()`.
MAX_AUDIT_ITERATIONS = 1
LOW_CONFIDENCE_THRESHOLD = 0.55

# Se a confiança média das fontes já coletadas está acima disto, a auditoria
# inteira (extração de claims + re-pesquisa) é pulada — não há o que ganhar
# auditando fontes que o próprio ConfidenceScorerV2 já considera confiáveis.
HIGH_CONFIDENCE_SKIP_THRESHOLD = 0.90

# Teto de gasto estimado (USD) por chamada de `audit()`. Cobre a extração de
# claims via LLM e as re-pesquisas de gap subsequentes.
DEFAULT_AUDIT_BUDGET_USD = 0.15

# Estimativa conservadora de custo por query de re-pesquisa de gap (busca +
# scoring downstream). Não é exata — é um teto de segurança, não um medidor.
ESTIMATED_COST_PER_GAP_QUERY_USD = 0.01


# ─── Data Contracts ──────────────────────────────────────────────────────────


@dataclass
class AuditClaim:
    """Uma afirmação extraída do relatório com seu status de validação."""

    text: str
    confidence: float = 0.0
    status: str = "unverified"  # verified | single_source | low_confidence | gap
    supporting_sources: list[str] = field(default_factory=list)
    needs_recheck: bool = False


@dataclass
class AuditReport:
    """Resultado completo de uma rodada de auditoria."""

    total_claims: int
    verified_claims: int
    low_confidence_claims: int
    gaps_detected: list[str]
    iterations_run: int
    enriched_content: str  # Relatório original + notas de auditoria injetadas
    audit_summary: str
    skipped: bool = False  # True se a auditoria foi pulada (alta confiança)
    budget_exhausted: bool = False  # True se parou por ter estourado o budget
    estimated_cost_usd: float = 0.0


# ─── ResearchAuditor ─────────────────────────────────────────────────────────


class ResearchAuditor:
    """
    Auditor autônomo que itera sobre um relatório de pesquisa e preenche gaps.

    Integração com o Orchestrator:
        auditor = ResearchAuditor(llm_client=orch.llm, orchestrator=orch)
        audit   = await auditor.audit(report_text, existing_results)
    """

    def __init__(
        self,
        llm_client: LLMClient,
        orchestrator: Any | None = None,
        confidence_scorer: Any | None = None,
        audit_budget_usd: float = DEFAULT_AUDIT_BUDGET_USD,
    ) -> None:
        self.llm = llm_client
        self.orchestrator = orchestrator
        self.confidence_scorer = confidence_scorer
        self.audit_budget_usd = audit_budget_usd

    # ── Entry Point ──────────────────────────────────────────────────────────

    async def audit(
        self,
        report_text: str,
        existing_results: list[SearchResult] | None = None,
        max_iterations: int = MAX_AUDIT_ITERATIONS,
    ) -> AuditReport:
        """
        Executa a auditoria completa de um relatório.

        Args:
            report_text:      Texto Markdown do relatório gerado.
            existing_results: Fontes já coletadas no pipeline principal.
            max_iterations:   Limite de rodadas de re-pesquisa (default: 1).

        Returns:
            AuditReport com status por claim, gaps e relatório enriquecido.
            Pode retornar antecipadamente (`skipped=True`) se a confiança
            média das fontes já é alta, ou parar cedo (`budget_exhausted=True`)
            se o orçamento da auditoria se esgotar.
        """
        logger.info("ResearchAuditor: iniciando auditoria...")

        all_results = list(existing_results or [])

        # ── Skip antecipado para resultados de alta confiança ───────────────
        avg_confidence = self._average_confidence(all_results)
        if all_results and avg_confidence >= HIGH_CONFIDENCE_SKIP_THRESHOLD:
            logger.info(
                f"ResearchAuditor: confiança média das fontes já é alta "
                f"({avg_confidence:.0%} >= {HIGH_CONFIDENCE_SKIP_THRESHOLD:.0%}) "
                "— pulando auditoria (economia de LLM + buscas)."
            )
            return AuditReport(
                total_claims=0,
                verified_claims=0,
                low_confidence_claims=0,
                gaps_detected=[],
                iterations_run=0,
                enriched_content=report_text,
                audit_summary=(
                    f"Auditoria pulada — confiança média das fontes já é "
                    f"alta ({avg_confidence:.0%})."
                ),
                skipped=True,
            )

        # ── Budget dedicado a esta chamada de audit() ────────────────────────
        audit_budget = Budget(max_cost_usd_session=self.audit_budget_usd)
        budget_exhausted = False

        iteration = 0
        claims = await self._extract_claims(report_text, audit_budget)
        logger.info(f"ResearchAuditor: {len(claims)} claims extraídas.")

        while iteration < max_iterations:
            iteration += 1
            claims = await self._validate_claims(claims, all_results)

            gaps = [c for c in claims if c.needs_recheck]
            logger.info(
                f"ResearchAuditor [iter {iteration}]: "
                f"{len(gaps)} gaps detectados de {len(claims)} claims."
            )

            if not gaps:
                logger.info(
                    "ResearchAuditor: nenhum gap restante — auditoria concluída."
                )
                break

            if audit_budget.is_over_session_budget():
                budget_exhausted = True
                logger.warning(
                    f"ResearchAuditor: orçamento da auditoria esgotado "
                    f"(${audit_budget.session_spent_usd:.4f} / "
                    f"${self.audit_budget_usd:.4f}) — interrompendo re-pesquisa."
                )
                break

            new_results = await self._research_gaps(gaps, audit_budget)
            all_results.extend(new_results)

            # Verifica se a re-pesquisa trouxe melhorias suficientes
            if not new_results:
                logger.info(
                    "ResearchAuditor: re-pesquisa não retornou resultados — interrompendo."
                )
                break

        verified = [c for c in claims if c.status == "verified"]
        low_conf = [
            c for c in claims if c.status in ("low_confidence", "single_source")
        ]
        remaining_gaps = [c.text for c in claims if c.needs_recheck]

        enriched = self._inject_audit_notes(report_text, claims)
        summary = self._build_summary(claims, iteration)
        if budget_exhausted:
            summary += " Orçamento da auditoria esgotado antes de concluir todos os gaps."

        return AuditReport(
            total_claims=len(claims),
            verified_claims=len(verified),
            low_confidence_claims=len(low_conf),
            gaps_detected=remaining_gaps,
            iterations_run=iteration,
            enriched_content=enriched,
            audit_summary=summary,
            budget_exhausted=budget_exhausted,
            estimated_cost_usd=audit_budget.session_spent_usd,
        )

    # ── Extração de Claims ───────────────────────────────────────────────────

    async def _extract_claims(
        self, report_text: str, audit_budget: Budget | None = None
    ) -> list[AuditClaim]:
        """Usa o LLM para extrair afirmações verificáveis do relatório."""
        prompt = (
            "You are a fact-checking assistant. Extract all verifiable factual claims "
            "from the following research report.\n\n"
            "Rules:\n"
            "- Only extract claims that can be verified against external sources.\n"
            "- Exclude opinions, predictions, and subjective statements.\n"
            "- Each claim must be a complete, self-contained sentence.\n"
            "- Return a JSON array of strings (the claims).\n\n"
            f"Report:\n{report_text[:6000]}\n\n"
            "Return ONLY a valid JSON array of strings."
        )

        schema = {"type": "array", "items": {"type": "string"}}

        self._record_estimated_cost(
            audit_budget, prompt, output_tokens=300, hint="audit:extract_claims"
        )

        try:
            raw_claims = await self.llm.generate_structured(
                prompt, schema, temperature=0.2
            )
            if isinstance(raw_claims, list):
                return [AuditClaim(text=str(c)) for c in raw_claims if c]
        except Exception as e:
            logger.warning(f"ResearchAuditor: falha na extração de claims: {e}")

        # Fallback: extrai frases que começam com dados numéricos ou termos factuais
        import re

        sentences = re.findall(r"[A-Z][^.!?\n]{20,150}[.!?]", report_text)
        filtered_sentences = []
        for s in sentences:
            s_clean = s.strip()
            if (
                "> Gerado em" in s_clean
                or "##" in s_clean
                or "---" in s_clean
                or not s_clean
            ):
                continue
            filtered_sentences.append(s_clean)
        return [
            AuditClaim(text=s) for s in filtered_sentences[:5]
        ]  # máx 5 claims para economizar LLM calls

    # ── Validação de Claims ──────────────────────────────────────────────────

    async def _validate_claims(
        self,
        claims: list[AuditClaim],
        results: list[SearchResult],
    ) -> list[AuditClaim]:
        """Cruza claims com os resultados disponíveis para estimar confiança."""
        if not results:
            for claim in claims:
                claim.status = "gap"
                claim.needs_recheck = True
            return claims

        # Constrói um corpus de snippets para cross-reference
        corpus = "\n".join(
            f"[{i}] {getattr(r, 'title', '')} — {(getattr(r, 'description', '') or '')[:200]}"
            for i, r in enumerate(results[:30])
        )

        for claim in claims:
            if claim.status == "verified":
                claim.needs_recheck = False
                continue

            # Heurística rápida: busca palavras-chave do claim no corpus
            keywords = [w for w in claim.text.split() if len(w) > 4]
            matches = sum(1 for kw in keywords if kw.lower() in corpus.lower())
            coverage = matches / max(len(keywords), 1)

            if coverage >= 0.5:
                claim.confidence = min(0.9, 0.5 + coverage * 0.5)
                claim.status = "verified"
                claim.needs_recheck = False
            elif coverage >= 0.2:
                claim.confidence = 0.3 + coverage * 0.5
                claim.status = "single_source"
                claim.needs_recheck = False
            else:
                claim.confidence = coverage * 0.3
                claim.status = "low_confidence"
                claim.needs_recheck = True

        return claims

    # ── Re-pesquisa de Gaps ──────────────────────────────────────────────────

    async def _research_gaps(
        self, gaps: list[AuditClaim], audit_budget: Budget | None = None
    ) -> list[SearchResult]:
        """
        Relança buscas focadas nas claims com gap de evidência.
        Usa o Orchestrator se disponível; retorna lista vazia caso contrário.
        Para antecipadamente se `audit_budget` estourar no meio da iteração.
        """
        if self.orchestrator is None:
            logger.debug(
                "ResearchAuditor: sem orchestrator — pulando re-pesquisa de gaps."
            )
            return []

        new_results: list[SearchResult] = []

        for claim in gaps[:5]:  # Limita a 5 claims por iteração para controle de custo
            if audit_budget is not None and audit_budget.is_over_session_budget():
                logger.warning(
                    "ResearchAuditor: orçamento esgotado — interrompendo gaps restantes desta iteração."
                )
                break

            gap_query = self._claim_to_query(claim.text)
            logger.info(f"ResearchAuditor: re-pesquisando gap → '{gap_query[:60]}'")

            try:
                expanded = [
                    type(
                        "ExpandedQuery",
                        (),
                        {
                            "query": gap_query,
                            "type": "fact_check",
                            "priority": "alta",
                            "rationale": f"audit gap: {claim.text[:60]}",
                        },
                    )()
                ]
                intent = type(
                    "IntentResult",
                    (),
                    {
                        "domain": type("Domain", (), {"value": "general"})(),
                        "intention": type("Intention", (), {"value": "verify"})(),
                        "urgency": "nao",
                        "confidence": "alta",
                    },
                )()

                source_plan = self.orchestrator.source_planner.plan(intent, expanded)
                results = await self.orchestrator._parallel_search(
                    expanded, source_plan, intent
                )
                new_results.extend(results[:5])

                if audit_budget is not None:
                    audit_budget.record(
                        UsageRecord(
                            model="search",
                            input_tokens=0,
                            output_tokens=0,
                            estimated_cost_usd=ESTIMATED_COST_PER_GAP_QUERY_USD,
                            query_hint=f"audit:gap_research:{claim.text[:40]}",
                        )
                    )

            except Exception as e:
                logger.warning(
                    f"ResearchAuditor: falha na re-pesquisa do gap '{gap_query[:40]}': {e}"
                )

        return new_results

    # ── Injeção de Notas de Auditoria ────────────────────────────────────────

    def _inject_audit_notes(self, report_text: str, claims: list[AuditClaim]) -> str:
        """
        Injeta um bloco de resumo de auditoria no final do relatório.
        Não altera o corpo do relatório para preservar a narrativa original.
        """
        verified_pct = (
            round(
                len([c for c in claims if c.status == "verified"]) / len(claims) * 100
            )
            if claims
            else 0
        )
        gaps = [c for c in claims if c.needs_recheck]

        lines = [
            "\n\n---\n",
            "## 🛡️ Auditoria de Claims (ResearchAuditor)\n",
            "| Métrica | Valor |",
            "|---|---|",
            f"| Total de claims analisadas | {len(claims)} |",
            f"| Claims verificadas | {len([c for c in claims if c.status == 'verified'])} ({verified_pct}%) |",
            f"| Claims de fonte única | {len([c for c in claims if c.status == 'single_source'])} |",
            f"| Claims com gap de evidência | {len(gaps)} |",
        ]

        if gaps:
            lines.append("\n### ⚠️ Claims não verificadas\n")
            for g in gaps[:10]:
                lines.append(f"- {g.text[:120]}")

        return report_text + "\n".join(lines)

    # ── Utilidades ───────────────────────────────────────────────────────────

    def _claim_to_query(self, claim_text: str) -> str:
        """Transforma uma claim em uma query de busca concisa."""
        # Remove pontuação terminal e trunca
        return claim_text.rstrip(".!?")[:100]

    def _average_confidence(self, results: list[SearchResult]) -> float:
        """Confiança média (`confidence_score`) das fontes já coletadas."""
        if not results:
            return 0.0
        scores = [getattr(r, "confidence_score", 0.0) or 0.0 for r in results]
        return sum(scores) / len(scores)

    def _record_estimated_cost(
        self,
        audit_budget: Budget | None,
        text: str,
        output_tokens: int,
        hint: str,
    ) -> None:
        """
        Estima e contabiliza o custo de uma chamada LLM no budget da auditoria,
        reaproveitando o `TokenEconomy` já anexado ao `LLMClient` (se houver).
        É uma estimativa pré-chamada (não temos os tokens reais de resposta
        antes de chamar) — suficiente para um teto de segurança, não uma
        medição exata de custo.
        """
        if audit_budget is None:
            return
        token_economy = getattr(self.llm, "token_economy", None)
        if not isinstance(token_economy, TokenEconomy):
            return
        input_tokens, cost = token_economy.estimate_cost(
            text, output_tokens=output_tokens
        )
        audit_budget.record(
            UsageRecord(
                model=token_economy.default_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
                query_hint=hint,
            )
        )

    def _build_summary(self, claims: list[AuditClaim], iterations: int) -> str:
        """Gera sumário textual da auditoria."""
        total = len(claims)
        verified = len([c for c in claims if c.status == "verified"])
        gaps = len([c for c in claims if c.needs_recheck])

        pct = round(verified / total * 100) if total else 0
        return (
            f"Auditoria concluída em {iterations} iteração(ões). "
            f"{verified}/{total} claims verificadas ({pct}%). "
            f"{gaps} gap(s) restante(s)."
        )
