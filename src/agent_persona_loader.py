"""
agent_persona_loader.py — Carregador de Personas de Agentes para o SRA.

Lê prompts de persona Markdown de `prompts/agents/` e os injeta em chamadas
LLM dos stages do pipeline, condicionalmente por modo de operação.

Regras:
  - Cache em memória com TTL de 10 minutos (evita releitura de disco).
  - Se o arquivo não existir, retorna string vazia sem levantar exceção.
  - Personas nunca são carregadas em modos com cost_optimization=True.
  - O conteúdo retornado é o corpo do .md sem frontmatter YAML.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PERSONA_CACHE_TTL_SECONDS = 600  # 10 minutos


class AgentPersonaLoader:
    """Carrega e cacheia prompts de persona Markdown para injeção em stages LLM.

    Attributes:
        prompts_dir: Caminho absoluto para o diretório de personas.
        _cache: Dicionário {nome_agente: (conteúdo, timestamp_load)}.
    """

    def __init__(self, prompts_dir: Optional[Path] = None) -> None:
        """Inicializa o carregador de personas."""
        if prompts_dir is None:
            here = Path(__file__).resolve().parent  # src/
            prompts_dir = here.parent / "prompts" / "agents"

        self.prompts_dir = Path(prompts_dir)
        self._cache: dict[str, tuple[str, float]] = {}

        logger.debug(
            "AgentPersonaLoader: diretório de personas = %s (existe=%s)",
            self.prompts_dir,
            self.prompts_dir.exists(),
        )

    def load(self, agent_name: str) -> str:
        """Retorna o conteúdo (sem frontmatter) da persona solicitada.

        Utiliza cache em memória com TTL de 10 minutos. Retorna string
        vazia se o arquivo não existir, sem levantar exceção.

        Args:
            agent_name: Nome do arquivo sem extensão (ex: "sage_strategy").

        Returns:
            Conteúdo Markdown da persona sem frontmatter YAML, ou "" se ausente.
        """
        now = time.monotonic()

        if agent_name in self._cache:
            content, load_time = self._cache[agent_name]
            if now - load_time < _PERSONA_CACHE_TTL_SECONDS:
                logger.debug("AgentPersonaLoader: cache hit para '%s'.", agent_name)
                return content

        file_path = self.prompts_dir / f"{agent_name}.md"
        if not file_path.exists():
            logger.warning(
                "AgentPersonaLoader: persona '%s' não encontrada em %s. "
                "Retornando string vazia.",
                agent_name,
                file_path,
            )
            return ""

        try:
            raw = file_path.read_text(encoding="utf-8")
            content = self._strip_frontmatter(raw)
            self._cache[agent_name] = (content, now)
            logger.info(
                "AgentPersonaLoader: persona '%s' carregada (%d chars).",
                agent_name,
                len(content),
            )
            return content
        except OSError as e:
            logger.error(
                "AgentPersonaLoader: erro ao ler '%s': %s. Retornando string vazia.",
                file_path,
                e,
            )
            return ""

    def build_enhanced_prompt(self, base_prompt: str, agent_name: str) -> str:
        """Injeta a persona no início de um prompt base existente.

        Args:
            base_prompt: Prompt original do stage.
            agent_name: Nome da persona a injetar.

        Returns:
            Prompt enriquecido com persona no início, separado por divisor.
            Retorna base_prompt inalterado se a persona não for encontrada.
        """
        persona_content = self.load(agent_name)
        if not persona_content:
            return base_prompt
        return f"{persona_content}\n\n---\n\n{base_prompt}"

    def is_persona_available(self, agent_name: str) -> bool:
        """Verifica se uma persona está disponível no disco."""
        return (self.prompts_dir / f"{agent_name}.md").exists()

    def clear_cache(self) -> None:
        """Limpa o cache em memória, forçando releitura do disco na próxima chamada."""
        self._cache.clear()
        logger.debug("AgentPersonaLoader: cache limpo.")

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """Remove frontmatter YAML delimitado por '---' do início do conteúdo."""
        pattern = r"^---\s*\n.*?\n---\s*\n"
        stripped = re.sub(pattern, "", content, count=1, flags=re.DOTALL)
        return stripped.lstrip()
