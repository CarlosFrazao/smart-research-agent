"""
verification_stage.py — Estágio de verificação de claims de código (Fase 1A).

Extrai blocos de código Python dos 5 melhores resultados rankeados e os
executa em sandbox Docker isolada via CodeExecutionAgent, armazenando os
resultados em `context.extra["verified_claims"]` para uso no estágio de
síntese e no relatório final.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from src.pipeline.pipeline import PipelineContext, PipelineStage
from src.services.code_execution_agent import CodeExecutionAgent
from src.agent_persona_loader import AgentPersonaLoader

logger = logging.getLogger("verification-stage")

# Número máximo de fontes a verificar por execução (evita timeout no pipeline)
_MAX_SOURCES_TO_VERIFY = 5
# Timeout por execução de código em segundos
_CODE_TIMEOUT_SECONDS = 10.0


class VerificationStage(PipelineStage):
    """
    Estágio de verificação de claims técnicas via sandbox Docker.

    Fluxo por execução:
    1. Itera pelos `_MAX_SOURCES_TO_VERIFY` primeiros `context.ranked_results`.
    2. Tenta extrair código Python via LLM (se disponível) ou via regex.
    3. Executa o código no CodeExecutionAgent via `loop.run_in_executor`
       (não bloqueia o event loop).
    4. Armazena os resultados (status, stdout, stderr) em
       `context.extra["verified_claims"]`.

    Se Docker não estiver disponível no host, o estágio registra um aviso
    e pula a verificação sem lançar exceção (comportamento gracioso).
    """

    name = "verification"

    def __init__(
        self,
        code_agent: CodeExecutionAgent | None = None,
        llm_client: Any = None,
        fact_checker: Any | None = None,
        ragas_evaluator: Any | None = None,
        trulens_recorder: Any | None = None,
    ) -> None:
        super().__init__()
        self.code_agent = code_agent or CodeExecutionAgent()
        self.llm = llm_client
        # QA instrumentation components (Fase 6.2). Injected for tests;
        # built lazily inside `run()` when the auditor is enabled and omitted here.
        self.fact_checker = fact_checker
        self.ragas_evaluator = ragas_evaluator
        self.trulens_recorder = trulens_recorder
        # Persona loader for Scout integration
        self.persona_loader = AgentPersonaLoader()

    async def run(self, context: PipelineContext) -> PipelineContext:
        logger.info("VerificationStage: iniciando verificação de claims de código.")

        results = context.ranked_results or []
        if not results:
            logger.info(
                "VerificationStage: nenhum resultado rankeado disponível. Pulando."
            )
            context.extra["verified_claims"] = []
        else:
            verified_claims: list[dict[str, Any]] = []
            top_results = results[:_MAX_SOURCES_TO_VERIFY]

            for res in top_results:
                source_text = (
                    f"Title: {res.title or ''}\nDescription: {res.description or ''}"
                )

                # Extração de código: LLM primeiro, regex como fallback
                code_to_run = await self._extract_code_with_llm(source_text)
                if not code_to_run or not code_to_run.strip():
                    code_to_run = self._extract_code_with_regex(source_text)

                if not code_to_run or not code_to_run.strip():
                    # Fonte sem código executável — ignora silenciosamente
                    continue

                logger.info(
                    "VerificationStage: executando claim de '%s' (%s).",
                    res.title or "sem título",
                    res.url or "sem URL",
                )

                try:
                    loop = asyncio.get_running_loop()
                    exec_res = await loop.run_in_executor(
                        None,
                        self.code_agent.execute_python,
                        code_to_run,
                        _CODE_TIMEOUT_SECONDS,
                    )

                    status = (
                        "verified"
                        if exec_res.exit_code == 0 and not exec_res.timed_out
                        else "failed"
                    )
                    verified_claims.append(
                        {
                            "title": res.title,
                            "url": res.url,
                            "code": code_to_run,
                            "stdout": exec_res.stdout,
                            "stderr": exec_res.stderr,
                            "exit_code": exec_res.exit_code,
                            "timed_out": exec_res.timed_out,
                            "status": status,
                            "error_message": exec_res.error_message,
                        }
                    )
                    logger.info(
                        "VerificationStage: %s (exit=%d, timed_out=%s).",
                        status,
                        exec_res.exit_code,
                        exec_res.timed_out,
                    )
                except Exception as e:
                    logger.warning(
                        "VerificationStage: erro ao rodar sandbox para '%s': %s",
                        res.title or "sem título",
                        e,
                    )
                    verified_claims.append(
                        {
                            "title": res.title,
                            "url": res.url,
                            "code": code_to_run,
                            "status": "error",
                            "error_message": str(e),
                        }
                    )

            context.extra["verified_claims"] = verified_claims
            logger.info(
                "VerificationStage: concluído. %d claim(s) processada(s).",
                len(verified_claims),
            )

            # Análise de arquitetura com Scout para repositórios GitHub
            repo_architectures: list[dict[str, str]] = []
            github_results = [r for r in results if r.url and "github.com" in r.url]

            if github_results and self.llm:
                logger.info(
                    "VerificationStage: analisando %d repositório(s) GitHub com Scout.",
                    len(github_results),
                )
                scout_tasks = [
                    self._analyze_github_repo_with_scout(
                        r.url or "",
                        f"Title: {r.title or ''}\nDescription: {r.description or ''}",
                    )
                    for r in github_results[:_MAX_SOURCES_TO_VERIFY]
                ]
                scout_results = await asyncio.gather(
                    *scout_tasks, return_exceptions=True
                )
                for result in scout_results:
                    if isinstance(result, dict) and result.get("architecture_map"):
                        repo_architectures.append(result)

            context.extra["repo_architectures"] = repo_architectures
            logger.info(
                "VerificationStage: %d mapa(s) de arquitetura gerado(s) com Scout.",
                len(repo_architectures),
            )

        # ── Camada de QA / Avaliação (Fase 6.2) ──────────────────────────
        # Só acionada quando o modo de operação ativo define `enable_auditor=True`
        # (ex.: "cirurgia", "black_ops", "arqueologia", "debate"). Modos rápidos
        # como "guerrilha" pulam esta etapa. Tudo é best-effort: falhas de QA
        # nunca abortam o pipeline nem mascaram o relatório.
        await self._run_qa_instrumentation(context)

        return context

    # ── Instrumentação de QA (Fase 6.2) ────────────────────────────────

    def _auditor_enabled(self, context: PipelineContext) -> bool:
        """Retorna True se o modo de operação ativo habilita o auditor."""
        orchestrator = context.extras.get("orchestrator") if context.extras else None
        operation_mode = (
            getattr(orchestrator, "operation_mode", None) if orchestrator else None
        )
        return bool(getattr(operation_mode, "enable_auditor", False))

    async def _run_qa_instrumentation(self, context: PipelineContext) -> None:
        """Executa a camada de QA/avaliação quando o auditor está habilitado.

        Reconecta três componentes órfãos do SRA (Fase 6.2):
          - ``MultiLLMFactChecker``: valida cruzada de claims via múltiplos LLMs.
          - ``RagasEvaluator``: métricas de qualidade (faithfulness, etc.).
          - ``TruLensRecorder``: gravação de runs de avaliação.

        As métricas são persistidas em ``context.extra``:
          - ``context.extra["ragas_metrics"]`` (dict por etapa)
          - ``context.extra["trulens_metrics"]`` (lista de QualityRecord)
          - ``context.extra["fact_check_results"]`` (lista de FactCheckResult)

        Best-effort: qualquer exceção é logada e ignorada; o pipeline
        prossegue para a próxima etapa.
        """
        if not self._auditor_enabled(context):
            logger.info(
                "VerificationStage: QA desativada (auditor off). Pulando instrumentação."
            )
            return

        logger.info("VerificationStage: QA ativada — executando instrumentação.")

        # 1. Fact-checking multi-LLM dos claims verificados
        fact_results = await self._run_fact_checker(context)
        if fact_results:
            context.extra["fact_check_results"] = [vars(r) for r in fact_results]

        # 2. RagasEvaluator (métricas de qualidade por etapa)
        ragas = self.ragas_evaluator or self._build_ragas_evaluator()
        if ragas is not None:
            try:
                metrics = await self._collect_ragas_metrics(context, ragas)
                if metrics:
                    context.extra.setdefault("ragas_metrics", {})
                    context.extra["ragas_metrics"].update(metrics)
            except Exception as e:  # pragma: no cover - defensivo
                logger.warning("VerificationStage: Ragas falhou (não-fatal): %s", e)

        # 3. TruLensRecorder (gravação de runs de avaliação)
        trulens = self.trulens_recorder or self._build_trulens_recorder()
        if trulens is not None:
            try:
                record = trulens.record(
                    "verification",
                    metrics=context.extra.get("ragas_metrics", {}).get("verification"),
                )
                context.extra.setdefault("trulens_metrics", []).append(
                    {
                        "stage_name": record.stage_name,
                        "timestamp": record.timestamp,
                        "metrics": record.metrics,
                        "passed": record.passed,
                        "error": record.error,
                    }
                )
            except Exception as e:  # pragma: no cover - defensivo
                logger.warning("VerificationStage: TruLens falhou (não-fatal): %s", e)

    async def _run_fact_checker(self, context: PipelineContext) -> list[Any]:
        """Roda o MultiLLMFactChecker sobre os claims verificados (se houver)."""
        checker = self.fact_checker or self._build_fact_checker()
        if checker is None:
            return []
        claims = [
            c.get("title") or c.get("url") or ""
            for c in (context.extra.get("verified_claims") or [])
            if isinstance(c, dict)
        ]
        claims = [c for c in claims if c]
        if not claims:
            logger.info("VerificationStage: nenhum claim para fact-check.")
            return []
        try:
            results = await checker.verify_batch(claims, concurrency=3)
            logger.info("VerificationStage: %d claim(s) fact-checked.", len(results))
            return results
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning("VerificationStage: fact-check falhou (não-fatal): %s", e)
            return []

    def _build_fact_checker(self) -> Any | None:
        """Constrói o MultiLLMFactChecker a partir do(s) LLM(s) disponível(is)."""
        if self.llm is None:
            return None
        try:
            from src.reasoning.multi_llm_fact_checker import MultiLLMFactChecker

            # Aceita tanto um único cliente quanto uma lista.
            llms = self.llm if isinstance(self.llm, (list, tuple)) else [self.llm]
            return MultiLLMFactChecker(llm_clients=list(llms))
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning("VerificationStage: falha ao criar FactChecker: %s", e)
            return None

    def _build_ragas_evaluator(self) -> Any | None:
        """Constrói o RagasEvaluator (lazy; import isolado)."""
        try:
            from src.evaluation.ragas_integration import RagasEvaluator

            return RagasEvaluator(enabled=True)
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning("VerificationStage: falha ao criar Ragas: %s", e)
            return None

    def _build_trulens_recorder(self) -> Any | None:
        """Constrói o TruLensRecorder (lazy; import isolado)."""
        try:
            from src.evaluation.trulens_integration import TruLensRecorder

            return TruLensRecorder(enabled=True)
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning("VerificationStage: falha ao criar TruLens: %s", e)
            return None

    async def _collect_ragas_metrics(
        self, context: PipelineContext, ragas: Any
    ) -> dict[str, Any]:
        """Coleta métricas Ragas para as etapas relevantes do pipeline."""
        from src.evaluation.ragas_integration import ragas_pipeline

        ctx = await ragas_pipeline(context, ragas, "verification")
        return ctx.extra.get("ragas_metrics", {})

    # ── Helpers de extração ──────────────────────────────────────────────────

    def _extract_code_with_regex(self, text: str) -> str | None:
        """Extrai o primeiro bloco ```python ... ``` encontrado via regex."""
        pattern = r"```python\s*(.*?)\s*```"
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        return match.group(1) if match else None

    async def _extract_code_with_llm(self, text: str) -> str | None:
        """
        Usa o LLM para identificar e extrair código Python executável do texto.
        Retorna None se nenhum código for encontrado ou se o LLM não estiver
        disponível.
        """
        if not self.llm:
            return None

        prompt = (
            "Você é um analisador estático de código. Analise o snippet abaixo "
            "extraído de uma pesquisa na web.\n\n"
            "Se ele contiver um exemplo de código Python que possa ser executado "
            "de forma autônoma como um script independente, retorne APENAS o código "
            "Python completo e pronto para execução (inclua os imports necessários).\n\n"
            "Se o código for incompleto, monte um script mínimo funcional que chame "
            "as funções com valores de exemplo razoáveis.\n\n"
            "Se o texto NÃO contiver código Python executável (código de outra "
            "linguagem, pseudocódigo, apenas texto), responda EXATAMENTE com: NENHUM\n\n"
            "REGRA: Não use crases (```) na resposta. Retorne apenas código Python "
            "puro ou a palavra NENHUM.\n\n"
            f"Texto:\n{text}\n"
        )

        try:
            raw = await self.llm.generate(prompt, temperature=0.1, max_tokens=800)
            clean = raw.strip()

            if not clean or clean.upper().startswith("NENHUM"):
                return None

            # Remove crases residuais se o LLM desobedecer a instrução
            if clean.startswith("```"):
                lines = clean.splitlines()
                lines = lines[1:] if lines[0].startswith("```") else lines
                lines = lines[:-1] if lines and lines[-1].strip() == "```" else lines
                clean = "\n".join(lines).strip()

            return clean or None
        except Exception as e:
            logger.warning("VerificationStage: _extract_code_with_llm falhou: %s", e)
            return None

    async def _analyze_github_repo_with_scout(
        self, repo_url: str, description: str
    ) -> dict[str, str]:
        """Ativa a persona Scout para mapear a arquitetura de um repositório concorrente.

        Opera APENAS sobre os dados já coletados (title + description + url).
        NÃO realiza novas chamadas HTTP.

        Args:
            repo_url: URL do repositório GitHub.
            description: Texto da descrição já disponível no result.

        Returns:
            Dicionário com 'url' e 'architecture_map' (string Markdown).
        """
        if not self.llm:
            return {"url": repo_url, "architecture_map": ""}

        scout_persona = self.persona_loader.load("scout_explorer")
        if not scout_persona:
            return {"url": repo_url, "architecture_map": ""}

        prompt = (
            f"{scout_persona}\n\n---\n\n"
            f"**Repositório:** {repo_url}\n\n"
            f"**Descrição disponível:**\n{description}\n\n"
            "Com base exclusivamente nas informações acima, produza o mapa arquitetural "
            "conforme o formato de saída definido. Se as informações forem insuficientes, "
            "declare 'Dados insuficientes para mapeamento arquitetural' e indique o nível "
            "de confiança como 'baixa'."
        )

        try:
            raw = await self.llm.generate(prompt, temperature=0.1, max_tokens=800)
            return {"url": repo_url, "architecture_map": raw.strip()}
        except Exception as e:
            logger.warning("VerificationStage: Scout falhou para '%s': %s", repo_url, e)
            return {"url": repo_url, "architecture_map": ""}
