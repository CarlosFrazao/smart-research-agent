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
    ) -> None:
        super().__init__()
        self.code_agent = code_agent or CodeExecutionAgent()
        self.llm = llm_client

    async def run(self, context: PipelineContext) -> None:
        logger.info("VerificationStage: iniciando verificação de claims de código.")

        results = context.ranked_results or []
        if not results:
            logger.info("VerificationStage: nenhum resultado rankeado disponível. Pulando.")
            context.extra["verified_claims"] = []
            return

        verified_claims: list[dict[str, Any]] = []
        top_results = results[:_MAX_SOURCES_TO_VERIFY]

        for res in top_results:
            source_text = (
                f"Title: {res.title or ''}\n"
                f"Description: {res.description or ''}"
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