"""Verificador de fatos multi-LLM usando multiplos modelos para validar afirmacoes de forma cruzada."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FactCheckResult:
    claim: str
    verdict: str
    confidence: float
    consensus: bool
    evidence_snippets: list[str] = field(default_factory=list)
    llm_verdicts: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""


class MultiLLMFactChecker:
    _VERDICT_PROMPT = (
        "You are a rigorous fact-checker. Analyze the claim against evidence.\n\n"
        "Claim: {claim}\n\nEvidence:\n{evidence}\n\n"
        "Respond EXACTLY:\n"
        "VERDICT: [supported|refuted|uncertain]\n"
        "CONFIDENCE: [0.0-1.0]\n"
        "REASONING: [one sentence]"
    )

    def __init__(self, llm_clients: list[Any], timeout: float = 30.0) -> None:
        if not llm_clients:
            raise ValueError("Need at least 1 LLM client.")
        self.llms = llm_clients
        self.timeout = timeout

    def _build_prompt(self, claim: str, evidence: list[str]) -> str:
        ev_block = (
            "\n".join(f"- {e}" for e in evidence[:10]) if evidence else "No evidence."
        )
        return self._VERDICT_PROMPT.format(claim=claim, evidence=ev_block)

    def _parse_response(self, text: str, name: str) -> dict[str, Any]:
        verdict, confidence, reasoning = "uncertain", 0.5, ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("VERDICT:"):
                v = line.split(":", 1)[1].strip().lower()
                if v in ("supported", "refuted", "uncertain"):
                    verdict = v
            elif line.startswith("CONFIDENCE:"):
                try:
                    confidence = max(
                        0.0, min(1.0, float(line.split(":", 1)[1].strip()))
                    )
                except ValueError:
                    pass
            elif line.startswith("REASONING:"):
                reasoning = line.split(":", 1)[1].strip()
        return {
            "llm": name,
            "verdict": verdict,
            "confidence": confidence,
            "reasoning": reasoning,
        }

    async def _call_llm(self, llm: Any, prompt: str) -> dict[str, Any] | None:
        name = getattr(llm, "model", getattr(llm, "name", "unknown"))
        try:
            text: str = await asyncio.wait_for(
                llm.complete(prompt), timeout=self.timeout
            )
            return self._parse_response(text, name)
        except (TimeoutError, Exception) as exc:
            logger.warning(f"FactChecker LLM {name!r} falhou: {exc}")
        return None

    def _calculate_consensus(self, verdicts: list[dict[str, Any]]):
        if not verdicts:
            return "uncertain", 0.0, False
        counts: dict[str, int] = {}
        total_conf: dict[str, float] = {}
        for v in verdicts:
            vd = v["verdict"]
            counts[vd] = counts.get(vd, 0) + 1
            total_conf[vd] = total_conf.get(vd, 0.0) + v["confidence"]
        dominant = max(counts, key=lambda k: (counts[k], total_conf[k]))
        n = counts[dominant]
        return dominant, total_conf[dominant] / n, n > len(verdicts) / 2

    async def verify_claim(
        self, claim: str, evidence: list[str] | None = None
    ) -> FactCheckResult:
        prompt = self._build_prompt(claim, evidence or [])
        raw = await asyncio.gather(*[self._call_llm(llm, prompt) for llm in self.llms])
        verdicts = [r for r in raw if r is not None]
        if not verdicts:
            return FactCheckResult(
                claim=claim,
                verdict="uncertain",
                confidence=0.0,
                consensus=False,
                evidence_snippets=evidence or [],
                reasoning="All LLMs failed.",
            )
        dominant, confidence, consensus = self._calculate_consensus(verdicts)
        top = next((v["reasoning"] for v in verdicts if v["verdict"] == dominant), "")
        return FactCheckResult(
            claim=claim,
            verdict=dominant,
            confidence=round(confidence, 3),
            consensus=consensus,
            evidence_snippets=(evidence or [])[:5],
            llm_verdicts=verdicts,
            reasoning=top,
        )

    async def verify_batch(
        self, claims: list[str], evidence: list[str] | None = None, concurrency: int = 3
    ) -> list[FactCheckResult]:
        sem = asyncio.Semaphore(concurrency)

        async def _one(c: str) -> FactCheckResult:
            async with sem:
                return await self.verify_claim(c, evidence)

        return list(await asyncio.gather(*[_one(c) for c in claims]))
