"""Detector de lacunas de cobertura em sessoes de pesquisa.

Analisa os resultados coletados e identifica aspectos nao cobertos,
gerando novas queries para iteracoes adicionais de busca.

Este modulo e chamado repetidamente dentro do loop de refinamento do
`Orchestrator` (ver `src/orchestrator.py`, "Passo 6-7/9"), que executa ate
`operation_mode.max_depth` iteracoes de: detectar gap -> buscar -> ranquear.
Sem controle de custo, esse loop pode rodar todas as iteracoes permitidas
mesmo quando (a) o orcamento de LLM/busca ja foi consumido ou (b) cada nova
iteracao esta trazendo resultados marginais. As tres salvaguardas abaixo
atacam exatamente isso:

  1. Orcamento acumulado: aborta o ciclo de gap-detection assim que o custo
     acumulado (proprio ou da sessao de LLM) ultrapassa o limite configurado.
  2. Retornos decrescentes: aborta se a ultima iteracao trouxe menos que
     `min_new_results_ratio` de resultados novos em relacao ao total anterior.
  3. Gap scoring: as queries de gap sugeridas sao pontuadas por probabilidade
     de trazerem resultados novos e relevantes, e apenas as melhores
     (`max_new_queries`) sao propagadas para a proxima iteracao de busca.
"""

import logging
from dataclasses import dataclass

from src.clients.llm_client import LLMClient
from src.types import GapAnalysis, IntentResult, RankedResult
from src.agent_persona_loader import AgentPersonaLoader

logger = logging.getLogger(__name__)

# Fracao minima de resultados novos entre iteracoes para que valha a pena
# continuar; abaixo disso consideramos "retornos decrescentes".
DEFAULT_MIN_NEW_RESULTS_RATIO = 0.05

# Orcamento (USD) dedicado ao proprio ciclo de gap-detection de UMA sessao
# de pesquisa. E independente do orcamento global do LLMClient (TokenEconomy),
# que tambem e respeitado quando disponivel — ver `_is_session_budget_exhausted`.
DEFAULT_MAX_BUDGET_USD = 0.20

# Numero maximo de queries de gap propagadas por iteracao, apos o gap scoring.
DEFAULT_MAX_NEW_QUERIES = 4

_CONFIDENCE_BASE_SCORE = {"alta": 0.9, "media": 0.6, "baixa": 0.35}


@dataclass
class GapDetectionState:
    """Estado (por sessao de pesquisa) do ciclo iterativo de gap-detection.

    Importante: o `GapDetector` costuma ser instanciado uma unica vez e
    reutilizado como singleton por multiplas pesquisas concorrentes (ver
    `Orchestrator.__init__` e o cache `_orchestrator` em `src/mcp_server.py`).
    Por isso o estado de orcamento acumulado e de contagem de resultados NAO
    deve viver como atributo de instancia do detector — isso vazaria estado
    entre sessoes/queries concorrentes e distintas. Em vez disso, o chamador
    (tipicamente `Orchestrator`) cria um `GapDetectionState` novo no inicio de
    cada pesquisa e o repassa a cada chamada de `detect()` daquela sessao.

    Se nenhum estado for passado, `detect()` cria um estado efemero valido
    apenas para aquela chamada — o comportamento observavel (heuristicas e
    resposta do LLM) continua correto, mas as protecoes que dependem de
    historico (orcamento acumulado entre iteracoes e retornos decrescentes)
    ficam limitadas a essa unica chamada.
    """

    previous_result_count: int = 0
    accumulated_cost_usd: float = 0.0
    iterations_run: int = 0


class GapDetector:
    """Detecta lacunas de cobertura na pesquisa usando heuristicas e LLM.

    Avalia se os resultados coletados cobrem os aspectos esperados da query
    e gera queries adicionais para fechar as lacunas identificadas, sempre
    respeitando um orcamento de custo e evitando iteracoes com retorno
    marginal desprezivel.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
        min_new_results_ratio: float = DEFAULT_MIN_NEW_RESULTS_RATIO,
        max_new_queries: int = DEFAULT_MAX_NEW_QUERIES,
    ):
        self.llm = llm_client
        self.max_iterations = 3
        self.max_budget_usd = max_budget_usd
        self.min_new_results_ratio = min_new_results_ratio
        self.max_new_queries = max_new_queries

        # Persona loader for GapDetector integration
        self.persona_loader = AgentPersonaLoader()

    async def detect(
        self,
        results: list[RankedResult],
        query: str,
        intent: IntentResult,
        state: GapDetectionState | None = None,
    ) -> GapAnalysis:
        """Detecta lacunas na cobertura dos resultados de pesquisa.

        Usa heuristicas rapidas antes de chamar o LLM para reduzir custo.
        Casos heuristicos: poucos resultados, poucas fontes, pouca diversidade.
        Antes de tudo, verifica orcamento acumulado e retornos decrescentes
        para decidir se vale a pena sequer continuar o ciclo de refinamento.

        Args:
            results: Lista de resultados ranqueados da pesquisa atual
                (acumulados ate esta iteracao).
            query: Query original do usuario.
            intent: Resultado da analise de intencao da query.
            state: Estado compartilhado entre as iteracoes de uma mesma
                sessao de pesquisa (orcamento acumulado, contagem anterior
                de resultados). Deve ser criado uma vez por sessao pelo
                chamador e reutilizado a cada chamada. Se omitido, um estado
                novo e efemero e usado (sem memoria entre iteracoes).

        Returns:
            GapAnalysis: Analise de lacunas com flag de completude,
                aspectos faltantes e novas queries sugeridas (priorizadas
                por probabilidade de trazerem resultados novos e relevantes).
        """
        state = state if state is not None else GapDetectionState()

        budget_gap = self._check_budget_exceeded(state)
        if budget_gap is not None:
            return budget_gap

        diminishing_gap = self._check_diminishing_returns(results, state)
        if diminishing_gap is not None:
            return diminishing_gap

        state.previous_result_count = len(results)
        state.iterations_run += 1

        source_coverage = len(set(r.source for r in results))
        top_projects = len(set(self._extract_project(r.title) for r in results[:20]))

        if len(results) < 10:
            return self._finalize(
                GapAnalysis(
                    is_complete=False,
                    missing_aspects=["poucos resultados"],
                    new_queries=[f"{query} open source", f"{query} alternative"],
                    confidence="alta",
                    rationale="Menos de 10 resultados encontrados",
                ),
                query,
            )

        if source_coverage < 3:
            return self._finalize(
                GapAnalysis(
                    is_complete=False,
                    missing_aspects=["cobertura de fontes insuficiente"],
                    new_queries=[f"{query} site:github.com", f"{query} reddit"],
                    confidence="media",
                    rationale=f"Apenas {source_coverage} fontes cobertas",
                ),
                query,
            )

        if top_projects < 3:
            return self._finalize(
                GapAnalysis(
                    is_complete=False,
                    missing_aspects=["pouca diversidade de projetos"],
                    new_queries=[f"best {query} 2026", f"{query} vs"],
                    confidence="media",
                    rationale="Menos de 3 projetos distintos nos top 20",
                ),
                query,
            )

        prompt_text = (
            "Voce e um auditor de qualidade de pesquisa tecnica.\n"
            "Analise os resultados e identifique lacunas.\n\n"
            "Criterios:\n"
            "1. Cobertura: principais fontes foram pesquisadas?\n"
            "2. Diversidade: resultados de projetos diferentes?\n"
            "3. Atualidade: resultados recentes (ultimos 12 meses)?\n"
            "4. Profundidade: ha analises comparativas, reviews?\n"
            "5. Conflitos: opinioes divergentes?\n\n"
            f"Query: {query}\n"
            f"Fontes cobertas: {source_coverage}\n"
            f"Projetos distintos (top 20): {top_projects}\n"
            f"Total resultados: {len(results)}\n\n"
            "Top 10 resultados:\n"
        )
        for i, r in enumerate(results[:10]):
            prompt_text += f"{i + 1}. [{r.source}] {r.title} (score: {r.score})\n"

        prompt_text += (
            "\nResponda em JSON:\n"
            "{\n"
            '  "is_complete": true,\n'
            '  "missing_aspects": ["string"],\n'
            '  "new_queries": ["string"],\n'
            '  "confidence": "alta|media|baixa",\n'
            '  "rationale": "string"\n'
            "}\n"
        )

        # Injetar Prism para rigor científico quando confiança < 0.75 ou modo exigente
        # Calcular overall_confidence a partir dos resultados (heurística simples)
        overall_confidence = min(1.0, len(results) * 0.1) if results else 0.5
        # Tentar obter operation_mode do orchestrator se disponível
        operation_mode = getattr(self, "_last_operation_mode", "cirurgia")  # fallback
        should_use_prism = overall_confidence < 0.75 or operation_mode in (
            "cirurgia",
            "black_ops",
        )
        if should_use_prism:
            prompt_text = self.persona_loader.build_enhanced_prompt(
                prompt_text, "prism_scientist"
            )
            logger.info(
                "GapDetector: persona Prism injetada (confidence=%.2f, mode=%s).",
                overall_confidence,
                operation_mode,
            )

        schema = {
            "type": "object",
            "properties": {
                "is_complete": {"type": "boolean"},
                "missing_aspects": {"type": "array", "items": {"type": "string"}},
                "new_queries": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": [
                "is_complete",
                "missing_aspects",
                "new_queries",
                "confidence",
                "rationale",
            ],
        }

        try:
            result = await self.llm.generate_structured(prompt_text, schema)
            self._track_llm_cost(state)
            return self._finalize(GapAnalysis(**result), query)
        except Exception as e:
            logger.warning(f"LLM gap detection falhou: {e}")
            return GapAnalysis(
                is_complete=True,
                missing_aspects=[],
                new_queries=[],
                confidence="media",
                rationale="Heuristicas indicam pesquisa suficiente",
            )

    # ── Orcamento acumulado ──────────────────────────────────────────────

    def _check_budget_exceeded(self, state: GapDetectionState) -> GapAnalysis | None:
        """Retorna um `GapAnalysis` de aborto se algum orcamento estourou.

        Verifica dois orcamentos independentes:
          1. O orcamento dedicado ao proprio ciclo de gap-detection
             (`self.max_budget_usd`), acumulado em `state.accumulated_cost_usd`.
          2. O orcamento global de sessao do `LLMClient` (`TokenEconomy`),
             quando disponivel — reaproveita a mesma infraestrutura de
             `src/token_economy.py` usada pelo restante do sistema.

        Retorna `None` se nao ha estouro (fluxo normal continua).
        """
        if state.accumulated_cost_usd > self.max_budget_usd:
            logger.warning(
                "GapDetector: orcamento dedicado excedido "
                f"(${state.accumulated_cost_usd:.4f} / ${self.max_budget_usd:.4f}) "
                f"apos {state.iterations_run} iteracao(oes); abortando ciclo."
            )
            return GapAnalysis(
                is_complete=True,
                missing_aspects=[],
                new_queries=[],
                confidence="baixa",
                rationale=(
                    f"Orcamento de gap-detection excedido "
                    f"(${state.accumulated_cost_usd:.4f} / ${self.max_budget_usd:.4f})"
                ),
            )

        session_economy = getattr(self.llm, "token_economy", None)
        if session_economy is not None:
            try:
                # Comparacao estrita com `True` (em vez de apenas truthy):
                # protege contra mocks/duck-typing em testes ou integracoes
                # onde `token_economy` nao segue o contrato real de
                # `TokenEconomy.budget.is_over_session_budget() -> bool`.
                # Preferimos falhar aberto (nao abortar) a um falso positivo.
                over_budget = session_economy.budget.is_over_session_budget()
            except Exception:
                over_budget = False
            if over_budget is True:
                logger.warning(
                    "GapDetector: orcamento de sessao do LLMClient esgotado; "
                    "abortando ciclo de gap-detection."
                )
                return GapAnalysis(
                    is_complete=True,
                    missing_aspects=[],
                    new_queries=[],
                    confidence="baixa",
                    rationale="Orcamento de sessao do LLMClient esgotado",
                )

        return None

    def _track_llm_cost(self, state: GapDetectionState) -> None:
        """Acumula em `state` o custo real da ultima chamada LLM, se disponivel.

        O `LLMClient.generate_structured` ja registra o uso real (tokens de
        entrada/saida, custo estimado) em `token_economy.budget.session_records`
        apos cada chamada — reaproveitamos esse registro em vez de estimar o
        custo novamente, para evitar contagem de tokens duplicada.
        """
        try:
            records = self.llm.token_economy.budget.session_records
            last_cost = records[-1].estimated_cost_usd
            state.accumulated_cost_usd += float(last_cost)
        except Exception:
            # LLMClient sem TokenEconomy real (ex.: mock em testes) ou
            # estrutura inesperada: instrumentacao e best-effort, nao deve
            # quebrar a deteccao de gaps.
            logger.debug("GapDetector: nao foi possivel rastrear custo da chamada LLM.")

    # ── Retornos decrescentes ────────────────────────────────────────────

    def _check_diminishing_returns(
        self, results: list[RankedResult], state: GapDetectionState
    ) -> GapAnalysis | None:
        """Aborta o ciclo se a iteracao anterior trouxe poucos resultados novos.

        So se aplica a partir da segunda iteracao (precisa de uma contagem
        anterior para comparar). Evita continuar re-pesquisando quando as
        novas queries geradas ja nao estao agregando cobertura relevante.
        """
        if state.iterations_run == 0 or state.previous_result_count == 0:
            return None

        current_count = len(results)
        new_results = current_count - state.previous_result_count
        growth_ratio = new_results / state.previous_result_count

        if growth_ratio < self.min_new_results_ratio:
            logger.info(
                "GapDetector: retornos decrescentes detectados "
                f"({growth_ratio:.1%} de resultados novos, limite "
                f"{self.min_new_results_ratio:.0%}); encerrando refinamento."
            )
            return GapAnalysis(
                is_complete=True,
                missing_aspects=[],
                new_queries=[],
                confidence="media",
                rationale=(
                    f"Retornos decrescentes: apenas {growth_ratio:.1%} de "
                    f"resultados novos desde a ultima iteracao "
                    f"(limite {self.min_new_results_ratio:.0%})"
                ),
            )

        return None

    # ── Gap scoring ───────────────────────────────────────────────────────

    def _finalize(self, gap: GapAnalysis, query: str) -> GapAnalysis:
        """Aplica gap scoring: prioriza e limita as queries de gap sugeridas."""
        if gap.new_queries:
            gap.new_queries = self._rank_and_trim_queries(
                gap.new_queries, query, gap.confidence
            )
        return gap

    def _rank_and_trim_queries(
        self, candidates: list[str], original_query: str, confidence: str
    ) -> list[str]:
        """Ordena queries de gap por probabilidade estimada e mantem as melhores.

        Mantem no maximo `self.max_new_queries`, o que tambem limita o custo
        (buscas + ranqueamento) gerado pela proxima iteracao.
        """
        scored = sorted(
            candidates,
            key=lambda q: self._score_gap_query(q, original_query, confidence),
            reverse=True,
        )
        return scored[: self.max_new_queries]

    def _score_gap_query(
        self, candidate: str, original_query: str, confidence: str
    ) -> float:
        """Estima a probabilidade de uma query de gap trazer resultados novos e relevantes.

        Combina tres sinais heuristicos, sem custo adicional de LLM:
          - confianca da analise que originou o gap (heuristicas
            deterministicas ja retornam "alta"/"media"; analise via LLM usa
            o campo `confidence` retornado por ele);
          - sobreposicao lexical com a query original (queries totalmente
            desconectadas do tema tendem a divergir e trazer ruido);
          - especificidade (queries muito curtas tendem a ser genericas
            demais e repetir resultados ja vistos).

        Retorna um score entre 0.0 e 1.0 (maior = mais provavel de agregar).
        """
        base = _CONFIDENCE_BASE_SCORE.get((confidence or "").lower(), 0.55)

        orig_tokens = {t for t in original_query.lower().split() if len(t) > 2}
        cand_tokens = {t for t in candidate.lower().split() if len(t) > 2}
        overlap = (
            len(orig_tokens & cand_tokens) / len(orig_tokens) if orig_tokens else 0.0
        )
        specificity = min(len(cand_tokens) / 4, 1.0)

        score = 0.6 * base + 0.25 * overlap + 0.15 * specificity
        return round(min(max(score, 0.0), 1.0), 4)

    def _extract_project(self, title: str) -> str:
        if "/" in title:
            return title.split("/")[-1]
        return title.split()[0] if title else ""
