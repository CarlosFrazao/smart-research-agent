"""CodeExecutionAgent — Sandbox de execução de código isolada em Docker.

Permite a execução segura e monitorada de trechos de código em containers
descartáveis (Docker) no host, mitigando ameaças de segurança como loops
infinitos, exfiltração de dados ou acessos indevidos.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from dataclasses import dataclass

logger = logging.getLogger("code-execution-agent")


@dataclass
class ExecutionResult:
    """Resultado da execução do código na sandbox."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    error_message: str = ""


class CodeExecutionAgent:
    """Sandbox isolada para execução de código Python usando Docker CLI."""

    def __init__(
        self, image: str = "python:3.11-slim", memory_limit: str = "128m"
    ) -> None:
        self.image = image
        self.memory_limit = memory_limit

    def execute_python(self, code: str, timeout: float = 5.0) -> ExecutionResult:
        """Executa um script Python em um container isolado sem acesso à rede.

        Args:
            code: Script Python a ser executado.
            timeout: Tempo limite de execução em segundos.

        Returns:
            ExecutionResult contendo saídas e status da sandbox.
        """
        container_name = f"sra-sandbox-{uuid.uuid4().hex[:8]}"
        cmd = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            container_name,
            "--network",
            "none",
            "--memory",
            self.memory_limit,
            self.image,
            "python",
            "-",
        ]

        logger.info(f"Iniciando sandbox {container_name} para execução de código...")

        try:
            proc = subprocess.run(
                cmd,
                input=code,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            return ExecutionResult(
                stdout=proc.stdout,
                stderr=proc.stderr,
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                f"Sandbox {container_name} excedeu o timeout de {timeout}s. Matando container..."
            )
            # Força o encerramento do container correspondente no host
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
            )
            return ExecutionResult(
                stdout="",
                stderr="",
                exit_code=-1,
                timed_out=True,
                error_message=f"Execution timed out after {timeout} seconds.",
            )
        except Exception as e:
            logger.error(f"Erro ao interagir com a sandbox Docker: {e}")
            return ExecutionResult(
                stdout="",
                stderr="",
                exit_code=-2,
                error_message=str(e),
            )
