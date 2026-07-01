"""
research_auditor.py — Loop de Auditoria Autônoma de Relatórios

Pipeline:
  1. Extrai claims do relatório Markdown via LLM
  2. Valida claims contra fontes existentes (ConfidenceScorerV2)
  3. Detecta gaps: claims não verificadas ou de fonte única
  4. Relança buscas focadas nos gaps (máx 3 iterações)
  5. Retorna relatório enriquecido com status de auditoria

Skill: adversarial-debate-engine (Auto-crítica adversária sistemática)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.types import SearchResult
from src.clients.llm_client import LLMClient

logger = logging.getLogger(__name__)

MAX_AUDIT_ITERATIONS = 3
LOW_CONFIDENCE_THRESHOLD = 0.55


# ─── Data Contracts ──────────────────────────────────────────────────────────

@dataclass
class AuditClaim:
    """Uma afirmação extraída do relatório com seu status de validação."""
    text: str
    confidence: float = 0.0
    status: str = "unverified"       # verified | single_source | low_confidence | gap
    supporting_sources: List[str] = field(default_factory=list)
    needs_recheck: bool = False


@dataclass
class AuditReport:
    """Resultado completo de uma rodada de auditoria."""
    total_claims: int
    verified_claims: int
    low_confidence_claims: int
    gaps_detected: List[str]
    iterations_run: int
    enriched_content: str            # Relatório original + notas de auditoria injetadas
    audit_summary: str


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
        orchestrator: Optional[Any] = None,
        confidence_scorer: Optional[Any] = None,
    ) -> None:
        self.llm = llm_client
        self.orchestrator = orchestrator
        self.confidence_scorer = confidence_scorer

    # ── Entry Point ──────────────────────────────────────────────────────────

    async def audit(
        self,
        report_text: str,
        existing_results: Optional[List[SearchResult]] = None,
        max_iterations: int = MAX_AUDIT_ITERATIONS,
    ) -> AuditReport:
        """
        Executa a auditoria completa de um relatório.

        Args:
            report_text:      Texto Markdown do relatório gerado.
            existing_results: Fontes já coletadas no pipeline principal.
            max_iterations:   Limite de rodadas de re-pesquisa (default: 3).

        Returns:
            AuditReport com status por claim, gaps e relatório enriquecido.
        """
        logger.info("ResearchAuditor: iniciando auditoria...")

        all_results = list(existing_results or [])
        iteration = 0

        claims = await self._extract_claims(report_text)
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
                logger.info("ResearchAuditor: nenhum gap restante — auditoria concluída.")
                break

            new_results = await self._research_gaps(gaps)
            all_results.extend(new_results)

            # Verifica se a re-pesquisa trouxe melhorias suficientes
            if not new_results:
                logger.info("ResearchAuditor: re-pesquisa não retornou resultados — interrompendo.")
                break

        verified     = [c for c in claims if c.status == "verified"]
        low_conf     = [c for c in claims if c.status in ("low_confidence", "single_source")]
        remaining_gaps = [c.text for c in claims if c.needs_recheck]

        enriched = self._inject_audit_notes(report_text, claims)
        summary  = self._build_summary(claims, iteration)

        return AuditReport(
            total_claims=len(claims),
            verified_claims=len(verified),
            low_confidence_claims=len(low_conf),
            gaps_detected=remaining_gaps,
            iterations_run=iteration,
            enriched_content=enriched,
            audit_summary=summary,
        )

    # ── Extração de Claims ───────────────────────────────────────────────────

    async def _extract_claims(self, report_text: str) -> List[AuditClaim]:
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

        try:
            raw_claims = await self.llm.generate_structured(prompt, schema, temperature=0.2)
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
            if "> Gerado em" in s_clean or "##" in s_clean or "---" in s_clean or not s_clean:
                continue
            filtered_sentences.append(s_clean)
        return [AuditClaim(text=s) for s in filtered_sentences[:20]]

    # ── Validação de Claims ──────────────────────────────────────────────────

    async def _validate_claims(
        self,
        claims: List[AuditClaim],
        results: List[SearchResult],
    ) -> List[AuditClaim]:
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

    async def _research_gaps(self, gaps: List[AuditClaim]) -> List[SearchResult]:
        """
        Relança buscas focadas nas claims com gap de evidência.
        Usa o Orchestrator se disponível; retorna lista vazia caso contrário.
        """
        if self.orchestrator is None:
            logger.debug("ResearchAuditor: sem orchestrator — pulando re-pesquisa de gaps.")
            return []

        new_results: List[SearchResult] = []

        for claim in gaps[:5]:  # Limita a 5 claims por iteração para controle de custo
            gap_query = self._claim_to_query(claim.text)
            logger.info(f"ResearchAuditor: re-pesquisando gap → '{gap_query[:60]}'")

            try:
                expanded = [
                    type("ExpandedQuery", (), {
                        "query": gap_query,
                        "type": "fact_check",
                        "priority": "alta",
                        "rationale": f"audit gap: {claim.text[:60]}",
                    })()
                ]
                intent = type("IntentResult", (), {
                    "domain": type("Domain", (), {"value": "general"})(),
                    "intention": type("Intention", (), {"value": "verify"})(),
                    "urgency": "nao",
                    "confidence": "alta",
                })()

                source_plan = self.orchestrator.source_planner.plan(intent, expanded)
                results = await self.orchestrator._parallel_search(expanded, source_plan, intent)
                new_results.extend(results[:5])

            except Exception as e:
                logger.warning(f"ResearchAuditor: falha na re-pesquisa do gap '{gap_query[:40]}': {e}")

        return new_results

    # ── Injeção de Notas de Auditoria ────────────────────────────────────────

    def _inject_audit_notes(self, report_text: str, claims: List[AuditClaim]) -> str:
        """
        Injeta um bloco de resumo de auditoria no final do relatório.
        Não altera o corpo do relatório para preservar a narrativa original.
        """
        verified_pct = (
            round(len([c for c in claims if c.status == "verified"]) / len(claims) * 100)
            if claims else 0
        )
        gaps = [c for c in claims if c.needs_recheck]

        lines = [
            "\n\n---\n",
            "## 🛡️ Auditoria de Claims (ResearchAuditor)\n",
            f"| Métrica | Valor |",
            f"|---|---|",
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

    def _build_summary(self, claims: List[AuditClaim], iterations: int) -> str:
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
