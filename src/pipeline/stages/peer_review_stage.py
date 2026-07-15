"""
Peer Review Stage (Bloco 9 / E4-T1) — Revisão de pares adversarial pós-síntese.

Estágio plugável do pipeline que aplica uma revisão de pares adversarial ao
relatório já sintetizado, combinando:

  1. **Checagens determinísticas locais** (sem chamadas HTTP), exigidas pela
     revisão crítica do Bloco 9:
       (a) claims sem ``source_id`` → marcados como ``UNSUPPORTED``;
       (b) contradições entre claims do mesmo relatório (mesmo tópico com
           polaridade oposta de confiança/afirmação) → ``CONTRADICTION``;
  2. **PeerReviewAgent** (``src/peer_review_agent.py``) existente, que soma
     sua análise heurística (superlativos não citados, seções curtas,
     citações para fora do conjunto de fontes) e sua revisão estruturada por
     LLM (quando disponível).

O estágio NÃO faz nenhuma chamada HTTP adicional — opera exclusivamente sobre
os dados já presentes no ``PipelineContext`` (``synthesized_claims`` e
``ranked_results``), honrando a regra de segurança do SRA (ver skill
``security-hardening``): o Peer Review nunca dispara nova rede.

É **não-bloqueante** (best-effort): falhas do agente LLM ou exceções nunca
abortam o pipeline. Os ``ReviewIssue`` detectados são serializados em
Markdown e expostos em ``context.extra["peer_review_section"]`` para que o
``ReportStage`` os anexe como ``## ⚠️ Limitações e Caveats (Peer Review)``.

Posição no pipeline: após ``synthesize`` (claims disponíveis) e idealmente
após ``quality_gate`` (scores já calculados), e antes de ``report``.
"""

from __future__ import annotations

import logging
from typing import Any

from src.pipeline.pipeline import PipelineContext, PipelineStage
from src.peer_review_agent import PeerReviewAgent, ReviewIssue

logger = logging.getLogger("pipeline.peer_review_stage")

# Severidade usada para as checagens determinísticas locais (Bloco 9).
_SEVERITY_MAJOR = "major"
_SEVERITY_CRITICAL = "critical"
_SEVERITY_MINOR = "minor"

_SECTION_TITLE = "## ⚠️ Limitações e Caveats (Peer Review)"


class PeerReviewStage(PipelineStage):
    """Estágio de revisão de pares adversarial pós-síntese.

    Args:
        agent: Instância de ``PeerReviewAgent`` (injetada; construída lazy se
            None a partir do ``llm_client`` dos deps da StageFactory).
        enabled: Se a revisão de pares deve rodar. Quando False, é no-op.
        config: Objeto ``Config`` do SRA (lê ``enable_peer_review`` /
            ``peer_review_*``). Injetado para testes; quando None, usa defaults.
    """

    name = "peer_review"
    critical = False  # best-effort: nunca aborta o pipeline

    def __init__(
        self,
        agent: PeerReviewAgent | None = None,
        *,
        enabled: bool = True,
        config: Any | None = None,
        llm_client: Any | None = None,
    ) -> None:
        self._agent = agent
        self.enabled = enabled
        self.config = config
        self._llm_client = llm_client

    def _build_agent(self) -> PeerReviewAgent | None:
        """Constrói o ``PeerReviewAgent`` a partir da config ou deps.

        Usa a instância injetada (``_agent``); senão tenta o ``llm_client``
        dos deps (injetado via construtor); senão None (apenas checagens
        determinísticas locais rodam).
        """
        if self._agent is not None:
            return self._agent
        llm = self._llm_client
        if llm is None and self.config is not None:
            llm = getattr(self.config, "llm_client", None)
        if llm is None:
            return None
        try:
            return PeerReviewAgent(llm_client=llm)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.warning(
                "PeerReviewStage: falha ao construir PeerReviewAgent: %s", exc
            )
            return None

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Aplica a revisão de pares e registra a seção em ``context.extra``."""
        if not self.enabled:
            logger.info("PeerReviewStage: desativado (enabled=False). Pulando.")
            context.extra["peer_review_section"] = ""
            context.extra["peer_review_issues"] = []
            return context

        logger.info(
            "PeerReviewStage: iniciando revisão de pares para query '%s'.",
            context.query[:60],
        )

        # 1. Checagens determinísticas locais (sem rede) — critério Claude Pro.
        local_issues = await self._detect_local_issues(context)

        # 2. Revisão estruturada via PeerReviewAgent (heurística + LLM).
        agent_issues: list[ReviewIssue] = []
        agent = self._build_agent()
        if agent is not None:
            try:
                report_text = context.report or self._build_report_text(context)
                known_results = list(context.ranked_results or []) or list(
                    context.raw_results or []
                )
                review = await agent.review(
                    report=report_text,
                    results=known_results,
                    query=context.query,
                )
                agent_issues = list(review.issues)
                context.extra["peer_review_assessment"] = review.overall_assessment
                context.extra["peer_review_confidence"] = review.confidence_in_report
            except Exception as exc:  # pragma: no cover - defensivo
                logger.warning(
                    "PeerReviewStage: PeerReviewAgent falhou (não-fatal): %s", exc
                )

        # 3. Merge (evita duplicar descrições iguais, case-insensitive).
        all_issues = list(local_issues)
        seen = {i.description.lower() for i in all_issues}
        for issue in agent_issues:
            if issue.description.lower() not in seen:
                all_issues.append(issue)
                seen.add(issue.description.lower())

        context.extra["peer_review_issues"] = all_issues
        context.extra["peer_review_section"] = self._render_section(all_issues)

        if all_issues:
            logger.info(
                "PeerReviewStage: %d issue(s) detectada(s) "
                "(%d crítica(s), %d maior(es), %d menor(es)).",
                len(all_issues),
                sum(1 for i in all_issues if i.severity == _SEVERITY_CRITICAL),
                sum(1 for i in all_issues if i.severity == _SEVERITY_MAJOR),
                sum(1 for i in all_issues if i.severity == _SEVERITY_MINOR),
            )
        else:
            logger.info("PeerReviewStage: nenhuma issue detectada.")

        return context

    # ── Checagens determinísticas locais ─────────────────────────────────────

    @staticmethod
    async def _detect_local_issues(context: PipelineContext) -> list[ReviewIssue]:
        """Aplica as checagens adversarial determinísticas (sem rede).

        (a) Claims sem ``source_ids`` → ``UNSUPPORTED`` (category
            ``unsupported_claim``).
        (b) Contradições entre claims do mesmo relatório (mesmo tópico com
            polaridade oposta) → ``CONTRADICTION`` (category ``bias``).

        Returns:
            list[ReviewIssue]: Issues locais detectadas (pode ser vazia).
        """
        issues: list[ReviewIssue] = []

        claims = list(context.extra.get("synthesized_claims") or [])
        if not claims:
            claims = await PeerReviewStage._derive_claims(context)

        # (a) Claims sem fonte.
        for claim in claims:
            source_ids = getattr(claim, "source_ids", None) or []
            text = (getattr(claim, "text", "") or "").strip()
            if text and not source_ids:
                issues.append(
                    ReviewIssue(
                        category="unsupported_claim",
                        severity=_SEVERITY_MAJOR,
                        description=(
                            "Afirmação sem fonte rastreável (source_id): "
                            f'"{text[:160]}"'
                        ),
                        location=text[:80],
                        suggestion=(
                            "Adicionar uma citação direta ([N](url)) ou remover "
                            "a afirmação não suportada do relatório."
                        ),
                    )
                )

        # (b) Contradições entre claims (mesmo tópico, polaridade oposta).
        contradictions = PeerReviewStage._detect_contradictions(claims)
        issues.extend(contradictions)

        return issues

    @staticmethod
    def _detect_contradictions(claims: list[Any]) -> list[ReviewIssue]:
        """Detecta pares de claims com o mesmo tópico e confiança oposta.

        Heurística: agrupa claims por uma palavra-tópico canônica (primeira
        palavra de conteúdo relevante) e, quando há dois claims do mesmo
        tópico com ``confidence`` de lados opostos da faixa (>= 0.5 vs < 0.5),
        marca contradição. Conservadora: só dispara com tópico idêntico e
        confiança divergente, evitando falso positivo em tópicos distintos.
        """
        by_topic: dict[str, list[Any]] = {}
        for claim in claims:
            text = (getattr(claim, "text", "") or "").strip()
            if not text:
                continue
            topic = PeerReviewStage._topic_key(text)
            if not topic:
                continue
            by_topic.setdefault(topic, []).append(claim)

        contradictions: list[ReviewIssue] = []
        for topic, group in by_topic.items():
            if len(group) < 2:
                continue
            hi = [c for c in group if PeerReviewStage._confidence(c) >= 0.5]
            lo = [c for c in group if PeerReviewStage._confidence(c) < 0.5]
            if not hi or not lo:
                continue
            hi_text = (getattr(hi[0], "text", "") or "").strip()[:120]
            lo_text = (getattr(lo[0], "text", "") or "").strip()[:120]
            contradictions.append(
                ReviewIssue(
                    category="bias",
                    severity=_SEVERITY_MAJOR,
                    description=(
                        f"Possível contradição interna sobre '{topic}': "
                        f'claim de alta confiança ("{hi_text}") vs. claim '
                        f'de baixa confiança ("{lo_text}").'
                    ),
                    location=topic,
                    suggestion=(
                        "Reconciliar as afirmações ou qualificar explicitamente "
                        "as condições sob as quais cada uma se aplica."
                    ),
                )
            )
        return contradictions

    @staticmethod
    def _topic_key(text: str) -> str:
        """Extrai uma palavra-tópico canônica (primeira substantiva relevante)."""
        stop = {
            "o",
            "a",
            "os",
            "as",
            "um",
            "uma",
            "de",
            "da",
            "do",
            "das",
            "dos",
            "em",
            "no",
            "na",
            "nos",
            "nas",
            "que",
            "e",
            "ou",
            "para",
            "por",
            "com",
            "se",
            "ao",
            "à",
            "the",
            "a",
            "an",
            "of",
            "in",
            "on",
            "to",
            "and",
            "or",
            "for",
            "with",
            "that",
            "is",
            "are",
            "was",
            "were",
        }
        for token in text.lower().split():
            token = token.strip(".,;:!?()[]{}\"'")
            if len(token) > 3 and token not in stop:
                return token
        return ""

    @staticmethod
    def _confidence(claim: Any) -> float:
        """Retorna a confiança do claim (0.0-1.0), default 1.0 se ausente."""
        value = getattr(claim, "confidence", None)
        if value is None:
            return 1.0
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 1.0
        if v > 1.0:
            v = v / 100.0
        return max(0.0, min(1.0, v))

    @staticmethod
    async def _derive_claims(context: PipelineContext) -> list[Any]:
        """Deriva claims sob demanda dos ``synthesized_results`` (Bloco 5).

        Usa ``Synthesizer._build_claim`` diretamente sobre cada
        ``SynthesizedResult`` (o tipo real de ``context.synthesized_results``
        no pipeline de produção, ver ``ReportStage.assemble_report``). Evita
        depender de ``synthesize_with_claims``, que exige ``RankedResult`` e
        quebraria com o tipo real — mantendo a derivação determinística e sem
        chamadas de rede.
        """
        synthesized = list(context.synthesized_results or [])
        if not synthesized:
            return []
        try:
            from src.synthesizer import Synthesizer

            claims = [Synthesizer._build_claim(r) for r in synthesized if r is not None]
            return list(claims)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.debug("PeerReviewStage: falha ao derivar claims: %s", exc)
            return []

    # ── Renderização ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_report_text(context: PipelineContext) -> str:
        """Monta um texto de relatório mínimo a partir dos resultados ranqueados.

        Usado como entrada para o PeerReviewAgent quando ``context.report``
        ainda está vazio (ex.: execução do estágio fora do ReportStage).
        """
        parts: list[str] = [f"# Relatório sobre: {context.query}\n"]
        for r in list(context.ranked_results or [])[:15]:
            title = getattr(r, "title", "") or ""
            desc = getattr(r, "description", "") or ""
            url = getattr(r, "url", "") or ""
            line = f"- {title}: {desc}".strip()
            if url:
                line += f" ({url})"
            parts.append(line)
        return "\n".join(parts)

    @staticmethod
    def _render_section(issues: list[ReviewIssue]) -> str:
        """Serializa as issues em Markdown (seção única do relatório)."""
        if not issues:
            return ""

        severity_labels = {
            _SEVERITY_CRITICAL: "🔴 critical",
            _SEVERITY_MAJOR: "🟠 major",
            _SEVERITY_MINOR: "🟡 minor",
        }

        lines = [_SECTION_TITLE, ""]
        lines.append(
            "> Revisão de pares adversarial aplicada ao relatório. As afirmações "
            "abaixo requerem verificação ou qualificação antes de serem tomadas "
            "como fato."
        )
        lines.append("")
        lines.append("| Categoria | Severidade | Descrição / Contexto | Sugestão |")
        lines.append("| :--- | :--- | :--- | :--- |")

        for issue in issues:
            sev = severity_labels.get(issue.severity, issue.severity)
            desc = (
                issue.description.replace("|", "\\|")
                .replace("\n", " ")
                .replace("\r", " ")
            )
            loc = ""
            if issue.location:
                loc = (
                    ' *"'
                    + issue.location.replace("|", "\\|")
                    .replace("\n", " ")
                    .replace("\r", " ")
                    + '"*'
                )
            sug = (
                issue.suggestion.replace("|", "\\|")
                .replace("\n", " ")
                .replace("\r", " ")
            )
            lines.append(f"| {issue.category} | {sev} | {desc}{loc} | {sug} |")

        return "\n".join(lines)
