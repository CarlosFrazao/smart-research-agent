import logging
import os
import shutil
from typing import Any

logger = logging.getLogger("orchestrator.report_service")


class ReportService:
    """
    Gerencia a geração de relatórios de pesquisa, formatação, exportação e sincronização com vaults.
    """

    def __init__(self, orchestrator):
        self.orch = orchestrator

    @property
    def config(self):
        return self.orch.config

    @property
    def report_generator(self):
        return self.orch.report_generator

    async def generate(self, query: str, synthesized: list[Any], metadata: Any) -> str:
        """
        Invoca o ReportGenerator para compilar as entidades em um relatório Markdown estruturado.
        """
        return await self.report_generator.generate(query, synthesized, metadata)

    def save(self, report: str, query: str, formats: list[Any] | None = None) -> str:
        """
        Salva o relatório fisicamente no disco nas extensões solicitadas.
        """
        return self.report_generator.save_report(
            report, query, self.config.output_dir, formats=formats
        )

    def sync_to_vault(self, filepath: str) -> None:
        """
        Sincroniza o relatório com o Obsidian Vault se habilitado nas configurações.
        """
        vault_dir = getattr(self.config, "obsidian_vault_path", None)
        auto_sync = getattr(self.config, "obsidian_auto_sync", False)

        if vault_dir and auto_sync:
            try:
                os.makedirs(vault_dir, exist_ok=True)
                vault_path = os.path.join(vault_dir, os.path.basename(filepath))
                shutil.copy2(filepath, vault_path)
                logger.info(f"Obsidian sync: {vault_path}")
            except Exception as e:
                logger.warning(f"Obsidian sync falhou (não crítico): {e}")
