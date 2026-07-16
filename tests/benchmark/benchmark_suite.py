"""
Benchmark automatizado SRA vs. Perplexity / Gemini (Bloco 15 / E1-T3).

Compara a qualidade da resposta do Smart Research Agent (SRA) contra
baselines comerciais (Perplexity API e Gemini API) usando as mesmas 20
queries-âncora em 5 domínios, avaliadas por RAGAS (via ``QualityGate`` do
Bloco 6 / E1-T2) e por recall derivado das ``SynthesizedClaim`` (Bloco 5 / E1-T1).

Modos de execução (protocolo ZEUS — nunca chama redes externas sem autorização):

    # Dry-run determinístico (padrão / CI): usa fixtures mockadas, zero rede.
    python -m tests.benchmark.benchmark_suite --dry-run

    # Live (requer API keys reais no ambiente):
    python -m tests.benchmark.benchmark_suite --live

Em ``--dry-run`` todos os três backends (SRA, Perplexity, Gemini) são
alimentados por fixtures JSONL em ``tests/benchmark/fixtures/`` — nenhuma
chamada HTTP real é feita. O relatório é escrito em
``benchmark_results/report_<data>.md``.

Este módulo é **aditivo**: não altera ``benchmark_queries.py`` nem os demais
testes de ``tests/benchmark/`` — funde com o existente (regra "fundir, não
sobrescrever").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = None  # import tardio para evitar custo de importação em CLI mínima


# ─── Queries âncora (20, 5 domínios) ─────────────────────────────────────────

ANCHOR_QUERIES: list[dict[str, str]] = [
    # tech
    {
        "domain": "tech",
        "query": "qual a diferença entre Rust e Go para serviços de backend?",
    },
    {
        "domain": "tech",
        "query": "melhor stack para automação de marketing open source 2026",
    },
    {"domain": "tech", "query": "n8n vs Make vs Zapier para orquestração de workflows"},
    {
        "domain": "tech",
        "query": "como funciona o modelo de concorrência async do Tokio em Rust",
    },
    # biomedical
    {
        "domain": "biomedical",
        "query": "ensaios clínicos recentes de terapia CAR-T para leucemia",
    },
    {
        "domain": "biomedical",
        "query": "efeitos adversos de inibidores de SGLT2 em diabetes tipo 2",
    },
    {
        "domain": "biomedical",
        "query": "marcadores genéticos de Alzheimer de início tardio",
    },
    {"domain": "biomedical", "query": "protocolos de fase 3 para doença de Parkinson"},
    # economics
    {
        "domain": "economics",
        "query": "impacto de políticas de jurosAltos sobre inflação em economias emergentes",
    },
    {
        "domain": "economics",
        "query": "relação entre educação e crescimento do PIB per capita",
    },
    {
        "domain": "economics",
        "query": "efeitos de tarifas comerciais no emprego manufatureiro",
    },
    {
        "domain": "economics",
        "query": "causas da estagnação da produtividade desde 2008",
    },
    # legal
    {
        "domain": "legal",
        "query": "direitos de privacidade de dados sob a LGPD no Brasil",
    },
    {
        "domain": "legal",
        "query": "precedentes da Suprema Corte sobre liberdade de expressão online",
    },
    {"domain": "legal", "query": "liability de IA sob a AI Act da União Europeia"},
    {
        "domain": "legal",
        "query": "requisitos de contrato inteligente sob a lei de propriedade",
    },
    # general
    {"domain": "general", "query": "como reduzir o consumo de energia de data centers"},
    {"domain": "general", "query": "tendências de mobilidade urbana elétrica em 2026"},
    {
        "domain": "general",
        "query": "impacto do microplástico na cadeia alimentar marinha",
    },
    {
        "domain": "general",
        "query": "princípios de design de jardins de chuva sustentáveis",
    },
]


# ─── Resultado de backend ────────────────────────────────────────────────────


@dataclass
class BackendAnswer:
    """Resposta normalizada de um backend de pesquisa.

    Attributes:
        backend: Nome do backend ("sra", "perplexity", "gemini").
        query: Query original.
        report: Texto da resposta (Markdown ou prosa).
        claims: Lista de afirmações com proveniência (lista de dicts com
            ``text``, ``source_ids``, ``urls``). Vazia se o backend não
            expõe claims estruturadas.
        contexts: Lista de snippets de contexto usados pela avaliação RAGAS.
        sources: Lista de URLs de fontes citadas (para cálculo de recall).
        latency_ms: Latência de ponta a ponta em milissegundos.
        error: Mensagem de erro (ou None se sucesso).
    """

    backend: str
    query: str
    report: str = ""
    claims: list[dict[str, Any]] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None and bool(self.report.strip())


# ─── Backends ────────────────────────────────────────────────────────────────


class Backend(ABC):
    """Backend de pesquisa comparável (SRA, Perplexity, Gemini)."""

    name: str = "abstract"

    @abstractmethod
    async def answer(self, query: str) -> BackendAnswer:
        """Executa a pesquisa e devolve uma ``BackendAnswer`` normalizada."""
        raise NotImplementedError


class SraBackend(Backend):
    """Backend do SRA.

    - ``dry_run=True``: lê fixture JSONL em ``fixtures/sra.jsonl`` (sem rede).
    - ``dry_run=False``: executa o ``Orchestrator`` real via
      ``create_orchestrator().research()`` e deriva claims/contexts do
      ``last_context`` (usa ``SynthesizedClaim`` do Bloco 5).
    """

    name = "sra"

    def __init__(self, dry_run: bool = True, fixture_path: Path | None = None) -> None:
        self.dry_run = dry_run
        self._fixture_path = fixture_path or (
            Path(__file__).parent / "fixtures" / "sra.jsonl"
        )
        self._fixtures: dict[str, dict[str, Any]] = {}
        if dry_run:
            self._load_fixtures()

    def _load_fixtures(self) -> None:
        if not self._fixture_path.exists():
            return
        for line in self._fixture_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            self._fixtures[obj["query"]] = obj

    async def answer(self, query: str) -> BackendAnswer:
        start = time.monotonic()
        if self.dry_run:
            answer = self._answer_dry(query)
        else:
            answer = await self._answer_live(query)
        answer.latency_ms = round((time.monotonic() - start) * 1000, 2)
        return answer

    def _answer_dry(self, query: str) -> BackendAnswer:
        obj = self._fixtures.get(query)
        if obj is None:
            return BackendAnswer(
                backend=self.name, query=query, error="fixture ausente para query"
            )
        return BackendAnswer(
            backend=self.name,
            query=query,
            report=obj.get("report", ""),
            claims=obj.get("claims", []),
            contexts=obj.get("contexts", []),
            sources=obj.get("sources", []),
        )

    async def _answer_live(self, query: str) -> BackendAnswer:
        try:
            from src.config import Config
            from src.orchestrator_factory import create_orchestrator

            config = Config()
            orchestrator = create_orchestrator(config)
            report = await orchestrator.research(query)
            ctx = getattr(orchestrator, "last_context", None)
            claims: list[Any] = []
            contexts: list[str] = []
            sources: list[str] = []
            if ctx is not None:
                synth = getattr(ctx, "synthesized_results", None) or []
                for r in synth:
                    cl = getattr(r, "claims", None)
                    if cl:
                        claims.extend(cl)  # SynthesizedClaim já são objetos reais
                ranked = getattr(ctx, "ranked_results", None) or []
                for r in ranked:
                    desc = getattr(r, "description", "") or ""
                    if desc.strip():
                        contexts.append(desc)
                    url = getattr(r, "url", "") or ""
                    if url:
                        sources.append(url)
            return BackendAnswer(
                backend=self.name,
                query=query,
                report=report,
                claims=claims,
                contexts=contexts,
                sources=sources,
            )
        except Exception as exc:  # pragma: no cover - live only
            return BackendAnswer(backend=self.name, query=query, error=str(exc))


class HttpCompetitorBackend(Backend):
    """Backend de competidor via HTTP (Perplexity / Gemini).

    Em ``dry_run`` lê fixture JSONL; em live faz a chamada HTTP real com a
    API key do ambiente (``PERPLEXITY_API_KEY`` / ``GEMINI_API_KEY``).
    Nunca é chamado em CI (somente via ``--live``).
    """

    api_env_key: str = ""
    endpoint: str = ""
    request_builder: Any = None  # callable(query) -> dict
    response_parser: Any = None  # callable(json) -> dict(text, sources)

    def __init__(self, dry_run: bool = True, fixture_path: Path | None = None) -> None:
        self.dry_run = dry_run
        self._fixture_path = fixture_path or (
            Path(__file__).parent / "fixtures" / f"{self.name}.jsonl"
        )
        self._fixtures: dict[str, dict[str, Any]] = {}
        if dry_run:
            self._load_fixtures()

    def _load_fixtures(self) -> None:
        if not self._fixture_path.exists():
            return
        for line in self._fixture_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            self._fixtures[obj["query"]] = obj

    async def answer(self, query: str) -> BackendAnswer:
        start = time.monotonic()
        if self.dry_run:
            answer = self._answer_dry(query)
        else:
            answer = await self._answer_live(query)
        answer.latency_ms = round((time.monotonic() - start) * 1000, 2)
        return answer

    def _answer_dry(self, query: str) -> BackendAnswer:
        obj = self._fixtures.get(query)
        if obj is None:
            return BackendAnswer(
                backend=self.name, query=query, error="fixture ausente para query"
            )
        return BackendAnswer(
            backend=self.name,
            query=query,
            report=obj.get("report", ""),
            claims=obj.get("claims", []),
            contexts=obj.get("contexts", []),
            sources=obj.get("sources", []),
        )

    async def _answer_live(self, query: str) -> BackendAnswer:
        import os

        import httpx

        api_key = os.environ.get(self.api_env_key, "")
        if not api_key:
            return BackendAnswer(
                backend=self.name,
                query=query,
                error=f"variável de ambiente {self.api_env_key} ausente",
            )
        try:
            payload = self.request_builder(query)
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
                resp.raise_for_status()
                parsed = self.response_parser(resp.json())
            return BackendAnswer(
                backend=self.name,
                query=query,
                report=parsed.get("text", ""),
                claims=parsed.get("claims", []),
                contexts=parsed.get("contexts", []),
                sources=parsed.get("sources", []),
            )
        except Exception as exc:  # pragma: no cover - live only
            return BackendAnswer(backend=self.name, query=query, error=str(exc))


class PerplexityBackend(HttpCompetitorBackend):
    """Perplexity API (modelo ``pplx-7b-online`` + online fallback)."""

    name = "perplexity"
    api_env_key = "PERPLEXITY_API_KEY"
    endpoint = "https://api.perplexity.ai/chat/completions"

    def __init__(self, dry_run: bool = True, fixture_path: Path | None = None) -> None:
        super().__init__(dry_run=dry_run, fixture_path=fixture_path)
        self.request_builder = self._build_request
        self.response_parser = self._parse_response

    @staticmethod
    def _build_request(query: str) -> dict[str, Any]:
        return {
            "model": "pplx-7b-online",
            "messages": [
                {"role": "system", "content": "Responda com citações de fontes."},
                {"role": "user", "content": query},
            ],
        }

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> dict[str, Any]:
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        citations = data.get("citations", []) or []
        return {"text": text, "sources": [c for c in citations if isinstance(c, str)]}


class GeminiBackend(HttpCompetitorBackend):
    """Google Gemini API (modelo ``gemini-1.5-pro`` via REST)."""

    name = "gemini"
    api_env_key = "GEMINI_API_KEY"
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-1.5-pro:generateContent"
    )

    def __init__(self, dry_run: bool = True, fixture_path: Path | None = None) -> None:
        super().__init__(dry_run=dry_run, fixture_path=fixture_path)
        self.request_builder = self._build_request
        self.response_parser = self._parse_response

    @staticmethod
    def _build_request(query: str) -> dict[str, Any]:
        return {
            "contents": [{"role": "user", "parts": [{"text": query}]}],
            "generationConfig": {"temperature": 0.2},
        }

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> dict[str, Any]:
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        return {"text": text, "sources": []}


# ─── Evaluator (RAGAS via QualityGate + recall de claims) ─────────────────────


@dataclass
class Evaluation:
    """Resultado da avaliação de uma ``BackendAnswer``."""

    backend: str
    query: str
    domain: str
    faithfulness: float = 0.0
    relevancy: float = 0.0
    traceability: float = 0.0
    recall: float = 0.0
    mode: str = "proxy"
    latency_ms: float = 0.0
    success: bool = False
    error: str | None = None
    sample_size: int = 0

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "query": self.query,
            "domain": self.domain,
            "faithfulness": round(self.faithfulness, 3),
            "relevancy": round(self.relevancy, 3),
            "traceability": round(self.traceability, 3),
            "recall": round(self.recall, 3),
            "mode": self.mode,
            "latency_ms": self.latency_ms,
            "success": self.success,
            "error": self.error,
            "sample_size": self.sample_size,
        }


def _build_claim_objects(raw_claims: list[Any]) -> list[Any]:
    """Constrói objetos ``SynthesizedClaim`` (Bloco 5) a partir de dicts/fixtures.

    Se a importação do modelo falhar (ambiente degradado), faz fallback para
    os próprios dicts (o evaluator de recall ainda funciona sobre dicts).
    """
    try:
        from src.types import SynthesizedClaim
    except Exception:  # pragma: no cover - defensivo
        return list(raw_claims)

    claims: list[Any] = []
    for c in raw_claims:
        if isinstance(c, SynthesizedClaim):
            claims.append(c)
            continue
        if isinstance(c, dict):
            try:
                claims.append(
                    SynthesizedClaim(
                        text=c.get("text", ""),
                        source_ids=list(c.get("source_ids") or []),
                        urls=list(c.get("urls") or []),
                        confidence=float(c.get("confidence", 1.0)),
                    )
                )
            except Exception:
                claims.append(c)
        else:
            claims.append(c)
    return claims


class Evaluator:
    """Avalia respostas com RAGAS (QualityGate) + recall de claims.

    O recall é derivado das claims (Bloco 5): fração de claims que citam ao
    menos uma fonte — proxy determinístico de cobertura de proveniência
    quando o RAGAS real não está instalado (igual ao QualityGate).
    """

    def __init__(self) -> None:
        try:
            from src.quality_gate import QualityGate

            self._gate = QualityGate()
        except Exception:  # pragma: no cover - defensivo
            self._gate = None

    async def evaluate(
        self, answer: BackendAnswer, query: str, domain: str
    ) -> Evaluation:
        ev = Evaluation(
            backend=answer.backend,
            query=query,
            domain=domain,
            latency_ms=answer.latency_ms,
            success=answer.success,
            error=answer.error,
        )
        if not answer.success:
            return ev

        # Constrói SynthesizedClaim reais (Bloco 5) para que o QualityGate
        # (Bloco 6) avalie com a API pública correta, em vez de dicts.
        claims = _build_claim_objects(answer.claims)
        contexts = list(answer.contexts)
        ev.sample_size = len(claims)

        if self._gate is not None:
            try:
                result = await self._gate.evaluate(query, claims, contexts)
                ev.faithfulness = result.faithfulness
                ev.relevancy = result.relevancy
                ev.traceability = result.traceability
                ev.mode = result.mode
            except Exception:  # pragma: no cover - defensivo
                pass

        ev.recall = self._recall_claims(claims)
        return ev

    @staticmethod
    def _recall_claims(claims: list[Any]) -> float:
        """Fração de claims com ao menos uma fonte rastreável (URL/source_id)."""
        if not claims:
            return 0.0
        grounded = 0
        for c in claims:
            sids = getattr(c, "source_ids", None) or (
                c.get("source_ids") if isinstance(c, dict) else []
            )
            urls = getattr(c, "urls", None) or (
                c.get("urls") if isinstance(c, dict) else []
            )
            if sids or urls:
                grounded += 1
        return grounded / len(claims)


# ─── Runner ───────────────────────────────────────────────────────────────────


class BenchmarkRunner:
    """Orquestra o benchmark comparativo entre backends."""

    def __init__(
        self,
        backends: list[Backend],
        queries: list[dict[str, str]] | None = None,
        dry_run: bool = True,
        out_dir: Path | None = None,
    ) -> None:
        self.backends = backends
        self.queries = queries or ANCHOR_QUERIES
        self.dry_run = dry_run
        self.out_dir = out_dir or (
            Path(__file__).parent.parent.parent / "benchmark_results"
        )
        self.evaluator = Evaluator()

    async def run(self) -> list[Evaluation]:
        evaluations: list[Evaluation] = []
        for q in self.queries:
            query = q["query"]
            domain = q.get("domain", "general")
            for backend in self.backends:
                answer = await backend.answer(query)
                ev = await self.evaluator.evaluate(answer, query, domain)
                evaluations.append(ev)
        return evaluations

    def render_report(self, evaluations: list[Evaluation]) -> str:
        """Gera o relatório Markdown comparativo."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        date = time.strftime("%Y-%m-%d")
        lines = [
            f"# 📊 Benchmark SRA vs. Perplexity/Gemini — {date}",
            "",
            f"- Modo: {'dry-run (fixtures, sem rede)' if self.dry_run else 'live'}",
            f"- Queries: {len(self.queries)} | Backends: {len(self.backends)}",
            "- Evaluator: QualityGate (RAGAS) + recall de claims (Bloco 5)",
            "",
            "## 📈 Resumo por Backend",
            "",
            "| Backend | Faithfulness | Relevancy | Traceability | Recall | Latência média (ms) | Sucesso |",
            "|---|---|---|---|---|---|---|",
        ]

        by_backend: dict[str, list[Evaluation]] = {}
        for ev in evaluations:
            by_backend.setdefault(ev.backend, []).append(ev)

        for backend in sorted(by_backend):
            evs = by_backend[backend]
            ok = [e for e in evs if e.success]
            n = len(ok) or 1
            faith = statistics.mean([e.faithfulness for e in ok]) if ok else 0.0
            rel = statistics.mean([e.relevancy for e in ok]) if ok else 0.0
            trac = statistics.mean([e.traceability for e in ok]) if ok else 0.0
            rec = statistics.mean([e.recall for e in ok]) if ok else 0.0
            lat = statistics.mean([e.latency_ms for e in evs]) if evs else 0.0
            succ = f"{len(ok)}/{len(evs)}"
            lines.append(
                f"| {backend} | {faith:.3f} | {rel:.3f} | {trac:.3f} | "
                f"{rec:.3f} | {lat:.1f} | {succ} |"
            )

        lines.extend(
            [
                "",
                "## 🔍 Detalhe por Query",
                "",
                "| Domínio | Query | Backend | Faith | Rel | Trace | Recall | ms | OK |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for ev in evaluations:
            ok = "✅" if ev.success else "❌"
            lines.append(
                f"| {ev.domain} | {ev.query[:40]} | {ev.backend} | "
                f"{ev.faithfulness:.2f} | {ev.relevancy:.2f} | {ev.traceability:.2f} | "
                f"{ev.recall:.2f} | {ev.latency_ms:.0f} | {ok} |"
            )

        lines.append("")
        lines.append("---")
        lines.append(
            "Relatório gerado pelo BenchmarkRunner do Bloco 15 (E1-T3). "
            "Em dry-run, todos os backends usam fixtures; nenhuma API externa foi chamada."
        )
        return "\n".join(lines)

    def write_report(self, evaluations: list[Evaluation]) -> Path:
        md = self.render_report(evaluations)
        date = time.strftime("%Y-%m-%d")
        path = self.out_dir / f"report_{date}.md"
        path.write_text(md, encoding="utf-8")
        return path


# ─── CLI ──────────────────────────────────────────────────────────────────────


def _build_backends(dry_run: bool) -> list[Backend]:
    return [
        SraBackend(dry_run=dry_run),
        PerplexityBackend(dry_run=dry_run),
        GeminiBackend(dry_run=dry_run),
    ]


async def _amain(dry_run: bool) -> int:
    runner = BenchmarkRunner(backends=_build_backends(dry_run), dry_run=dry_run)
    evaluations = await runner.run()
    path = runner.write_report(evaluations)
    print(f"\nRelatório escrito em: {path}")
    print(f"Total de avaliações: {len(evaluations)}")
    ok = sum(1 for e in evaluations if e.success)
    print(f"Sucesso: {ok}/{len(evaluations)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark SRA vs Perplexity/Gemini")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Usa fixtures mockadas (padrão, sem rede).",
    )
    group.add_argument(
        "--live",
        dest="live",
        action="store_true",
        help="Executa pesquisas reais (requer API keys no ambiente).",
    )
    args = parser.parse_args(argv)
    dry_run = not args.live
    return asyncio.run(_amain(dry_run))


if __name__ == "__main__":
    sys.exit(main())
