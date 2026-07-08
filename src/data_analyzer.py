"""
data_analyzer.py — Análise Quantitativa com Pandas via Sandbox Docker (Fase 2).

Esta funcionalidade reutiliza o `CodeExecutionAgent` (sandbox Docker isolada já
robusta do SRA) para gerar e executar scripts Pandas sobre dados crus
(CSVs/JSONs) extraídos das fontes de pesquisa — realizando análise
quantitativa real (market share, benchmarks de performance, agregações
estatísticas) e não apenas verificação de claims de código.

Fluxo:
  1. O usuário (ou o pipeline) fornece um conjunto de arquivos de dados crus
     e uma pergunta analítica.
  2. Um LLM (opcional) gera um script Pandas autocontido que carrega os dados
     e responde à pergunta, imprimindo o resultado.
  3. O script é executado na sandbox Docker (`--network none`, `--cap-drop ALL`,
     usuário `nobody`) via `CodeExecutionAgent`.
  4. O resultado (stdout/stderr/status) é retornado estruturado para a UI/CLI.

Se o LLM não estiver disponível, um gerador heurístico de fallback produz um
script Pandas básico de inspeção (cabeçalho, shape, describe) para que a
funcionalidade nunca quebre silenciosamente.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.services.code_execution_agent import CodeExecutionAgent, ExecutionResult

logger = logging.getLogger("data_analyzer")

# Número máximo de arquivos de dados suportados por análise
_MAX_DATA_FILES = 5
# Timeout padrão para execução do script Pandas na sandbox
_PANDAS_TIMEOUT_SECONDS = 30.0
# Tamanho máximo de cada arquivo de dados (8 MB) para não estourar a sandbox
_MAX_FILE_BYTES = 8 * 1024 * 1024


@dataclass
class DataAnalysisResult:
    """Resultado estruturado de uma análise quantitativa."""

    question: str
    script: str
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    status: str = "unknown"
    error_message: str = ""
    files_analyzed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serializa o resultado para consumo pela UI/CLI/API."""
        return {
            "question": self.question,
            "script": self.script,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "status": self.status,
            "error_message": self.error_message,
            "files_analyzed": self.files_analyzed,
        }


class DataAnalyzer:
    """
    Gera e executa scripts Pandas em sandbox Docker sobre dados crus.

    Reutiliza `CodeExecutionAgent` para isolamento total. Opcionalmente usa
    um `LLMClient` para gerar o script analítico; sem LLM, aplica um gerador
    de fallback determinístico.
    """

    def __init__(
        self,
        code_agent: CodeExecutionAgent | None = None,
        llm_client: Any = None,
    ) -> None:
        self.code_agent = code_agent or CodeExecutionAgent()
        self.llm = llm_client

    # ── API Pública ──────────────────────────────────────────────────────────

    def analyze(
        self,
        data_paths: list[str],
        question: str,
        timeout: float = _PANDAS_TIMEOUT_SECONDS,
    ) -> DataAnalysisResult:
        """
        Executa uma análise quantitativa Pandas sobre os arquivos fornecidos.

        Args:
            data_paths: Caminhos para CSVs/JSONs brutos.
            question: Pergunta analítica (ex.: "qual o market share por empresa?").
            timeout: Timeout de execução na sandbox em segundos.

        Returns:
            DataAnalysisResult com script, saídas e status.
        """
        valid_paths = self._filter_valid_paths(data_paths)
        if not valid_paths:
            logger.warning("DataAnalyzer: nenhum arquivo de dados válido fornecido.")
            return DataAnalysisResult(
                question=question,
                script="",
                stdout="",
                stderr="",
                exit_code=-4,
                status="error",
                error_message="Nenhum arquivo de dados válido foi fornecido.",
                files_analyzed=[],
            )

        logger.info(
            "DataAnalyzer: gerando script Pandas para %d arquivo(s). Pergunta: %s",
            len(valid_paths),
            question[:60],
        )

        script = self._generate_script(valid_paths, question)
        exec_res = self._execute(script, timeout)

        status = self._derive_status(exec_res)
        names = [Path(p).name for p in valid_paths]
        logger.info("DataAnalyzer: análise concluída — status=%s.", status)

        return DataAnalysisResult(
            question=question,
            script=script,
            stdout=exec_res.stdout,
            stderr=exec_res.stderr,
            exit_code=exec_res.exit_code,
            timed_out=exec_res.timed_out,
            status=status,
            error_message=exec_res.error_message,
            files_analyzed=names,
        )

    # ── Geração de Script ─────────────────────────────────────────────────────

    def _generate_script(self, data_paths: list[str], question: str) -> str:
        """Gera o script Pandas (LLM se disponível, senão fallback heurístico)."""
        llm_script = self._generate_script_with_llm(data_paths, question)
        if llm_script and llm_script.strip():
            return llm_script
        return self._fallback_script(data_paths, question)

    def _generate_script_with_llm(
        self, data_paths: list[str], question: str
    ) -> str | None:
        """Usa o LLM para gerar um script Pandas autocontido que responde à pergunta."""
        if not self.llm:
            return None

        file_descriptions = "\n".join(
            f"  - /data/{Path(p).name} (formato {Path(p).suffix or 'desconhecido'})"
            for p in data_paths
        )

        prompt = (
            "Você é um engenheiro de dados especialista em Pandas. Escreva um script "
            "Python autocontido que responda à pergunta abaixo analisando os arquivos "
            "de dados fornecidos.\n\n"
            "REGRAS OBRIGATÓRIAS:\n"
            "1. Os arquivos estarão montados no diretório /data/ dentro da sandbox.\n"
            "2. Use apenas pandas e numpy (já instalados na imagem python:3.11-slim).\n"
            "3. O script DEVE imprimir (print) o resultado da análise de forma legível.\n"
            "4. Não use requests, urllib, open() em rede ou qualquer acesso externo.\n"
            "5. Não use crases (```) na resposta. Retorne apenas código Python puro.\n\n"
            "Arquivos disponíveis:\n"
            f"{file_descriptions}\n\n"
            f"Pergunta: {question}\n\n"
            "Responda APENAS com o código Python que resolve a pergunta:"
        )

        try:
            raw = self.llm.generate(prompt, temperature=0.1, max_tokens=1200)
            clean = self._strip_code_fences(raw.strip())
            return clean or None
        except Exception as e:
            logger.warning("DataAnalyzer: falha ao gerar script via LLM: %s", e)
            return None

    def _fallback_script(self, data_paths: list[str], question: str) -> str:
        """Gera um script Pandas de inspeção básica quando não há LLM disponível."""
        load_lines: list[str] = []
        for idx, p in enumerate(data_paths):
            name = Path(p).name
            suffix = Path(p).suffix.lower()
            var = f"df{idx}"
            if suffix == ".json":
                load_lines.append(f'{var} = pd.read_json("/data/{name}")')
            else:
                load_lines.append(f'{var} = pd.read_csv("/data/{name}")')

        joined = "\n".join(load_lines)
        return (
            "import pandas as pd\n"
            "import numpy as np\n"
            "\n"
            f"{joined}\n"
            "\n"
            "print('=== Pergunta ===')\n"
            f"print({question!r})\n"
            "print()\n"
            "for i, df in enumerate([df0]):\n"
            "    pass\n"
            "print('=== Inspeção dos dados ===')\n"
            "dfs = [df0]\n"
            "for idx, df in enumerate(dfs):\n"
            "    print(f'--- Arquivo {idx} (shape={df.shape}) ---')\n"
            "    print(df.head(10).to_string())\n"
            "    print()\n"
            '    print(df.describe(include="all").to_string())\n'
            "    print()\n"
            "print('=== Colunas disponíveis ===')\n"
            "for idx, df in enumerate(dfs):\n"
            "    print(f'Arquivo {idx}: {list(df.columns)}')\n"
        )

    # ── Execução na Sandbox ───────────────────────────────────────────────────

    def _execute(self, script: str, timeout: float) -> ExecutionResult:
        """Executa o script na sandbox Docker isolada."""
        try:
            return self.code_agent.execute_python(script, timeout=timeout)
        except Exception as e:
            logger.error("DataAnalyzer: erro ao executar sandbox: %s", e)
            return ExecutionResult(
                stdout="",
                stderr="",
                exit_code=-2,
                error_message=str(e),
            )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _filter_valid_paths(self, data_paths: list[str]) -> list[str]:
        """Filtra caminhos que existem, são arquivos e não excedem o tamanho máximo."""
        valid: list[str] = []
        for p in data_paths[:_MAX_DATA_FILES]:
            try:
                path = Path(p)
                if path.is_file() and path.stat().st_size <= _MAX_FILE_BYTES:
                    valid.append(str(path))
                else:
                    logger.warning("DataAnalyzer: ignorando arquivo inválido: %s", p)
            except Exception as e:
                logger.warning("DataAnalyzer: erro ao validar '%s': %s", p, e)
        return valid

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Remove cercas ```python ... ``` residuais de respostas de LLM."""
        if not text.startswith("```"):
            return text
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @staticmethod
    def _derive_status(result: ExecutionResult) -> str:
        """Deriva um status legível a partir do ExecutionResult."""
        if result.timed_out:
            return "timeout"
        if result.exit_code == 0:
            return "success"
        return "failed"
