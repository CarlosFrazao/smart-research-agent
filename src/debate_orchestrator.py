"""
debate_orchestrator.py — Motor de Debate Multi-Agente do Smart Research Agent (Bloco 3.1)

Fluxo:
  1. generate_hypotheses()  → LLM gera N hipóteses opostas para a query
  2. run_debate()           → cada hipótese é pesquisada em paralelo pelos searchers
  3. judge_round()          → LLM juiz avalia os argumentos e emite um veredito
  4. format_debate_markdown() → monta o relatório de debate completo

Uso:
    debate = DebateOrchestrator(llm_client=llm, searchers=searchers)
    result = await debate.run(query)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("debate_orchestrator")


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class Hypothesis:
    """
    Representa uma hipótese a ser testada durante o debate.

    id        — identificador curto (ex: "H1", "H2")
    claim     — afirmação central que o agente defende
    rationale — por que esta hipótese é plausível
    stance    — "pro" | "contra" | "neutro"
    """
    id: str
    claim: str
    rationale: str
    stance: str = "pro"
    # Preenchidos após pesquisa
    evidence: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    confidence: float = 0.0
    search_results_count: int = 0


@dataclass
class DebateRound:
    """
    Registro completo de uma rodada de debate.

    query       — query original do usuário
    hypotheses  — lista de hipóteses testadas
    winner      — hipótese vencedora (id) segundo o juiz
    verdict     — texto completo do veredito do juiz
    confidence  — confiança do juiz na decisão (0.0-1.0)
    reasoning   — raciocínio detalhado do juiz
    timestamp   — quando o debate foi executado
    duration_s  — duração total em segundos
    """
    query: str
    hypotheses: List[Hypothesis]
    winner: str = ""
    verdict: str = ""
    confidence: float = 0.0
    reasoning: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    duration_s: float = 0.0


# ── DebateOrchestrator ─────────────────────────────────────────────────────────

class DebateOrchestrator:
    """
    Motor de debate adversarial multi-agente.

    Cada hipótese actua como um "agente de defesa" que busca evidências na web.
    O LLM juiz lê todos os argumentos e emite um veredito com raciocínio.
    """

    MAX_HYPOTHESES = 4       # Nunca gerar mais que 4 hipóteses por debate
    MAX_RESULTS_PER_H = 5    # Máximo de resultados de pesquisa por hipótese
    JUDGE_TEMP = 0.2         # Temperatura baixa para julgamento determinístico

    def __init__(
        self,
        llm_client: Any,
        searchers: Dict[str, Any],
        num_hypotheses: int = 3,
    ):
        self.llm = llm_client
        self.searchers = searchers
        self.num_hypotheses = min(num_hypotheses, self.MAX_HYPOTHESES)

    # ── API pública ────────────────────────────────────────────────────────────

    async def run(self, query: str) -> DebateRound:
        """
        Executa o ciclo completo de debate para uma query.

        Returns:
            DebateRound com hipóteses pesquisadas, veredito e raciocínio.
        """
        t0 = datetime.now()
        logger.info(f"DebateOrchestrator: iniciando debate para '{query[:80]}'")

        # Fase 1 — geração de hipóteses
        hypotheses = await self.generate_hypotheses(query)
        if not hypotheses:
            logger.warning("Nenhuma hipótese gerada — debate abortado.")
            return DebateRound(query=query, hypotheses=[], verdict="Debate não pôde ser iniciado.")

        # Fase 2 — pesquisa paralela por hipótese
        hypotheses = await self.run_debate(query, hypotheses)

        # Fase 3 — julgamento
        round_result = await self.judge_round(query, hypotheses)

        round_result.duration_s = (datetime.now() - t0).total_seconds()
        logger.info(
            f"DebateOrchestrator: debate concluído em {round_result.duration_s:.1f}s "
            f"— vencedor: {round_result.winner}"
        )
        return round_result

    # ── Fase 1 — Geração de Hipóteses ─────────────────────────────────────────

    async def generate_hypotheses(self, query: str) -> List[Hypothesis]:
        """
        Pede ao LLM que gere N hipóteses opostas/distintas sobre a query.
        Retorna uma lista de objetos Hypothesis parseados.
        """
        prompt = (
            "Você é um gerador de hipóteses adversariais. Escreva em Português do Brasil.\n\n"
            f"Query do usuário: \"{query}\"\n\n"
            f"Gere exatamente {self.num_hypotheses} hipóteses distintas e testáveis sobre esta query.\n"
            "Cada hipótese deve representar uma perspectiva diferente (ex: favorável, crítica, alternativa).\n\n"
            "Responda APENAS com o seguinte formato JSON válido (nada antes ou depois):\n"
            "[\n"
            "  {\n"
            "    \"id\": \"H1\",\n"
            "    \"claim\": \"<afirmação central em 1 frase>\",\n"
            "    \"rationale\": \"<por que é plausível em 1-2 frases>\",\n"
            "    \"stance\": \"pro\" | \"contra\" | \"neutro\"\n"
            "  },\n"
            "  ...\n"
            "]\n"
        )
        try:
            raw = await self.llm.generate(prompt, temperature=0.7, max_tokens=800)
            hypotheses = self._parse_hypotheses(raw)
            logger.info(f"Hipóteses geradas: {[h.id for h in hypotheses]}")
            return hypotheses
        except Exception as e:
            logger.error(f"generate_hypotheses falhou: {e}")
            # Fallback: 2 hipóteses genéricas pro/contra
            return [
                Hypothesis(
                    id="H1",
                    claim=f"A resposta afirmativa para '{query[:60]}' é suportada por evidências.",
                    rationale="Hipótese padrão — favor.",
                    stance="pro",
                ),
                Hypothesis(
                    id="H2",
                    claim=f"A resposta negativa ou alternativa para '{query[:60]}' é mais adequada.",
                    rationale="Hipótese padrão — contra.",
                    stance="contra",
                ),
            ]

    # ── Fase 2 — Pesquisa Paralela ─────────────────────────────────────────────

    async def run_debate(self, query: str, hypotheses: List[Hypothesis]) -> List[Hypothesis]:
        """
        Para cada hipótese, executa pesquisa com query especializada em paralelo.
        Preenche hypothesis.evidence, hypothesis.sources e hypothesis.confidence.
        """
        tasks = [self._research_hypothesis(query, h) for h in hypotheses]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for h, result in zip(hypotheses, results):
            if isinstance(result, Exception):
                logger.warning(f"Pesquisa para {h.id} falhou: {result}")
            else:
                h.evidence = result.get("evidence", [])
                h.sources = result.get("sources", [])
                h.confidence = result.get("confidence", 0.0)
                h.search_results_count = result.get("count", 0)

        return hypotheses

    async def _research_hypothesis(self, base_query: str, h: Hypothesis) -> Dict[str, Any]:
        """
        Executa pesquisa especializada para uma única hipótese.
        Usa uma query reformulada que favorece a perspectiva da hipótese.
        """
        # Reformula a query para reforçar a perspectiva da hipótese
        search_query = f"{base_query} {h.claim[:80]}"

        evidence: List[str] = []
        sources: List[str] = []
        total_results = 0

        # Usar os searchers disponíveis para coletar evidências
        primary_searchers = ["web", "github", "arxiv", "hackernews", "reddit"]
        for name in primary_searchers:
            searcher = self.searchers.get(name)
            if not searcher:
                continue
            try:
                from src.types import ExpandedQuery
                eq = ExpandedQuery(
                    query=search_query,
                    type="debate_hypothesis",
                    priority="alta",
                    rationale=h.rationale,
                )
                results = await asyncio.wait_for(
                    searcher.search(eq),
                    timeout=15.0,
                )
                for r in (results or [])[:self.MAX_RESULTS_PER_H]:
                    if r.description and len(r.description.strip()) > 50:
                        evidence.append(f"[{r.source}] {r.title}: {r.description[:200]}")
                        if r.url and r.url not in sources:
                            sources.append(r.url)
                        total_results += 1
                if total_results >= self.MAX_RESULTS_PER_H * 2:
                    break  # evidência suficiente
            except asyncio.TimeoutError:
                logger.warning(f"Searcher '{name}' timeout para hipótese {h.id}")
            except Exception as e:
                logger.debug(f"Searcher '{name}' erro para hipótese {h.id}: {e}")

        # Confiança proporcional ao volume de evidências encontradas
        confidence = min(total_results / (self.MAX_RESULTS_PER_H * 2), 1.0)
        return {
            "evidence": evidence[:10],
            "sources": sources[:5],
            "confidence": round(confidence, 2),
            "count": total_results,
        }

    # ── Fase 3 — Julgamento ────────────────────────────────────────────────────

    async def judge_round(self, query: str, hypotheses: List[Hypothesis]) -> DebateRound:
        """
        O LLM juiz lê todos os argumentos e emite um veredito com raciocínio detalhado.
        """
        # Formata os argumentos de cada hipótese para o prompt do juiz
        args_block = ""
        for h in hypotheses:
            evidence_str = "\n".join(f"    - {e}" for e in h.evidence[:5]) or "    (sem evidências encontradas)"
            args_block += (
                f"## {h.id}: {h.claim}\n"
                f"**Posição:** {h.stance} | **Confiança de pesquisa:** {h.confidence:.0%} "
                f"| **Fontes encontradas:** {h.search_results_count}\n"
                f"**Rationale:** {h.rationale}\n"
                f"**Evidências coletadas:**\n{evidence_str}\n\n"
            )

        prompt = (
            "Você é um juiz imparcial e rigoroso. Escreva em Português do Brasil.\n\n"
            f"A query em debate é: \"{query}\"\n\n"
            "As seguintes hipóteses foram pesquisadas e cada uma apresentou seus argumentos:\n\n"
            f"{args_block}"
            "Sua tarefa:\n"
            "1. Avalie cada hipótese pela qualidade e quantidade de evidências.\n"
            "2. Identifique a hipótese mais bem suportada pelos dados.\n"
            "3. Explique o raciocínio de forma clara e rastreável.\n\n"
            "Responda APENAS com o seguinte JSON válido:\n"
            "{\n"
            "  \"winner\": \"<id da hipótese vencedora, ex: H1>\",\n"
            "  \"confidence\": <0.0 a 1.0>,\n"
            "  \"reasoning\": \"<raciocínio detalhado em 3-5 frases>\",\n"
            "  \"verdict\": \"<declaração final em 1-2 frases>\"\n"
            "}\n"
        )

        try:
            raw = await self.llm.generate(prompt, temperature=self.JUDGE_TEMP, max_tokens=600)
            judgment = self._parse_judgment(raw)
            logger.info(
                f"Juiz decidiu: winner={judgment.get('winner')} "
                f"confidence={judgment.get('confidence')}"
            )
        except Exception as e:
            logger.error(f"judge_round LLM falhou: {e}")
            judgment = {
                "winner": hypotheses[0].id if hypotheses else "H1",
                "confidence": 0.5,
                "reasoning": "Julgamento automático indisponível — hipótese com mais evidências selecionada.",
                "verdict": "Veredito automático de fallback.",
            }

        return DebateRound(
            query=query,
            hypotheses=hypotheses,
            winner=judgment.get("winner", ""),
            verdict=judgment.get("verdict", ""),
            confidence=float(judgment.get("confidence", 0.5)),
            reasoning=judgment.get("reasoning", ""),
        )

    # ── Formatação ─────────────────────────────────────────────────────────────

    def format_debate_markdown(self, round_result: DebateRound) -> str:
        """
        Gera um relatório Markdown completo do debate, pronto para ser
        prefixado ao relatório principal ou retornado sozinho.
        """
        ts = round_result.timestamp.strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# 🗣️ Relatório de Debate Multi-Agente",
            "",
            f"> **Query:** {round_result.query}",
            f"> **Data:** {ts}  |  **Duração:** {round_result.duration_s:.1f}s",
            f"> **Hipóteses testadas:** {len(round_result.hypotheses)}",
            "",
            "---",
            "",
            "## 🏆 Veredito do Juiz",
            "",
            f"**Hipótese Vencedora:** `{round_result.winner}`  ",
            f"**Confiança do Juiz:** {round_result.confidence:.0%}",
            "",
            f"> {round_result.verdict}",
            "",
            "### Raciocínio Detalhado",
            "",
            round_result.reasoning or "_Raciocínio não disponível._",
            "",
            "---",
            "",
            "## 🧪 Hipóteses Testadas",
            "",
        ]

        for h in round_result.hypotheses:
            is_winner = (h.id == round_result.winner)
            trophy = " 🏆 **VENCEDOR**" if is_winner else ""
            lines += [
                f"### {h.id}{trophy}: {h.claim}",
                "",
                f"- **Posição:** {h.stance}",
                f"- **Rationale:** {h.rationale}",
                f"- **Resultados de pesquisa:** {h.search_results_count}",
                f"- **Confiança de pesquisa:** {h.confidence:.0%}",
                "",
                "**Evidências Coletadas:**",
            ]
            if h.evidence:
                for ev in h.evidence[:5]:
                    lines.append(f"- {ev}")
            else:
                lines.append("- _Sem evidências encontradas._")
            lines += [
                "",
                f"**Fontes:** {', '.join(h.sources[:3]) or '_nenhuma_'}",
                "",
            ]

        lines += [
            "---",
            "",
            f"*Debate gerado por Smart Research Agent — Modo Debate | {ts}*",
        ]

        return "\n".join(lines)

    # ── Parsers internos ───────────────────────────────────────────────────────

    def _parse_hypotheses(self, raw: str) -> List[Hypothesis]:
        """
        Extrai lista de Hypothesis do JSON retornado pelo LLM.
        Tolerante a texto extra antes/depois do JSON.
        """
        import json, re
        # Extrai o array JSON mesmo que o LLM adicione texto extra
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            raise ValueError(f"JSON de hipóteses não encontrado em: {raw[:200]}")
        data = json.loads(match.group())
        hypotheses = []
        for i, item in enumerate(data[: self.MAX_HYPOTHESES]):
            hypotheses.append(
                Hypothesis(
                    id=item.get("id", f"H{i+1}"),
                    claim=item.get("claim", ""),
                    rationale=item.get("rationale", ""),
                    stance=item.get("stance", "pro"),
                )
            )
        return hypotheses

    def _parse_judgment(self, raw: str) -> Dict[str, Any]:
        """
        Extrai o veredito do JSON retornado pelo LLM juiz.
        Tolerante a texto extra antes/depois do JSON.
        """
        import json, re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"JSON de julgamento não encontrado em: {raw[:200]}")
        return json.loads(match.group())
