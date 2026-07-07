"""CodeExecutionAgent — Sandbox de execução de código isolada em Docker.

Permite a execução segura e monitorada de trechos de código em containers
descartáveis (Docker) no host, mitigando ameaças de segurança como loops
infinitos, fork bombs, exfiltração de dados, escrita indevida em disco e
escalonamento de privilégios dentro do container.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from dataclasses import dataclass

logger = logging.getLogger("code-execution-agent")

# Limite de saida capturada por stream, para evitar que um script malicioso
# (ex: print("x" * 10**9)) estoure a memoria do processo pai via capture_output.
MAX_OUTPUT_CHARS = 64_000

# Timeout curto para comandos administrativos do Docker CLI (kill/inspect),
# independente do timeout de execucao do codigo do usuario.
_ADMIN_CMD_TIMEOUT = 10.0


@dataclass
class ExecutionResult:
    """Resultado da execucao do codigo na sandbox."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    error_message: str = ""


class CodeExecutionAgent:
    """Sandbox isolada para execucao de codigo Python usando Docker CLI.

    Camadas de isolamento aplicadas em cada execucao:
      - --network none: sem acesso a rede (exfiltracao de dados).
      - --memory + --memory-swap iguais: limite real de memoria, sem
        contornar via swap do host.
      - --cpus: limite de CPU para conter loops que consomem 100% de um core.
      - --pids-limit: contem fork bombs e explosao de processos/threads.
      - --cap-drop ALL + --security-opt no-new-privileges: remove
        capabilities do Linux e impede escalonamento de privilegios.
      - --read-only + tmpfs em /tmp: filesystem raiz imutavel, com uma
        area de escrita efemera e sem permissao de execucao.
      - --user: processo roda como usuario nao-root (nobody) dentro do
        container, mitigando o impacto de uma eventual fuga de container.
      - timeout do processo + docker kill: contem loops infinitos.
    """

    def __init__(
        self,
        image: str = "python:3.11-slim",
        memory_limit: str = "128m",
        cpus: str = "0.5",
        pids_limit: int = 64,
        user: str = "65534:65534",  # nobody:nogroup
    ) -> None:
        self.image = image
        self.memory_limit = memory_limit
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.user = user

        if shutil.which("docker") is None:
            logger.warning(
                "Binario 'docker' nao encontrado no PATH do host. "
                "execute_python() falhara com erro claro em vez de travar."
            )

    def _build_docker_cmd(self, container_name: str) -> list[str]:
        """Monta o comando docker run com todas as camadas de isolamento."""
        return [
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
            "--memory-swap",
            self.memory_limit,  # igual ao memory: desativa uso de swap
            "--cpus",
            self.cpus,
            "--pids-limit",
            str(self.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--user",
            self.user,
            self.image,
            "python",
            "-",
        ]

    @staticmethod
    def _truncate(text: str) -> str:
        if len(text) <= MAX_OUTPUT_CHARS:
            return text
        return (
            text[:MAX_OUTPUT_CHARS]
            + f"\n... [output truncado, limite de {MAX_OUTPUT_CHARS} caracteres]"
        )

    def _kill_container(self, container_name: str) -> None:
        """Forca o encerramento do container, sem deixar o comando pendurar."""
        try:
            proc = subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                text=True,
                timeout=_ADMIN_CMD_TIMEOUT,
            )
            if proc.returncode != 0:
                # Comum quando o container ja terminou sozinho (race condition
                # entre o timeout do processo pai e o fim natural do script).
                logger.debug(
                    f"'docker kill {container_name}' retornou {proc.returncode}: "
                    f"{proc.stderr.strip()}"
                )
        except subprocess.TimeoutExpired:
            logger.error(
                f"'docker kill {container_name}' nao respondeu em {_ADMIN_CMD_TIMEOUT}s. "
                "Daemon Docker pode estar sobrecarregado ou travado."
            )
        except Exception as e:
            logger.error(f"Erro ao tentar matar container {container_name}: {e}")

    def execute_python(self, code: str, timeout: float = 10.0) -> ExecutionResult:
        """Executa um script Python em um container isolado sem acesso a rede.

        Args:
            code: Script Python a ser executado.
            timeout: Tempo limite de execucao em segundos.

        Returns:
            ExecutionResult contendo saidas e status da sandbox.
        """
        if not code or not code.strip():
            return ExecutionResult(
                stdout="",
                stderr="",
                exit_code=-4,
                error_message="Codigo vazio: nenhuma sandbox foi iniciada.",
            )

        if shutil.which("docker") is None:
            return ExecutionResult(
                stdout="",
                stderr="",
                exit_code=-3,
                error_message=(
                    "Docker nao encontrado no host. Instale o Docker CLI "
                    "ou verifique se esta no PATH."
                ),
            )

        container_name = f"sra-sandbox-{uuid.uuid4().hex[:8]}"
        cmd = self._build_docker_cmd(container_name)

        logger.info(f"Iniciando sandbox {container_name} para execucao de codigo...")

        try:
            proc = subprocess.run(
                cmd,
                input=code,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            return ExecutionResult(
                stdout=self._truncate(proc.stdout),
                stderr=self._truncate(proc.stderr),
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                f"Sandbox {container_name} excedeu o timeout de {timeout}s. Matando container..."
            )
            self._kill_container(container_name)
            return ExecutionResult(
                stdout="",
                stderr="",
                exit_code=-1,
                timed_out=True,
                error_message=f"Execution timed out after {timeout} seconds.",
            )
        except FileNotFoundError as e:
            # Ocorre se o binario docker sumir do PATH entre o check acima e o run
            # (condicao de corrida rara, mas real em ambientes com PATH dinamico).
            logger.error(f"Binario Docker nao encontrado ao executar: {e}")
            return ExecutionResult(
                stdout="",
                stderr="",
                exit_code=-3,
                error_message="Docker nao encontrado no host durante a execucao.",
            )
        except Exception as e:
            logger.error(f"Erro ao interagir com a sandbox Docker: {e}")
            return ExecutionResult(
                stdout="",
                stderr="",
                exit_code=-2,
                error_message=str(e),
            )
