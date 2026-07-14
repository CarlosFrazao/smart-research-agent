"""Carregador de configuração runtime a partir de ``config/*.yaml``.

Centraliza a leitura de arquivos de configuração que antes eram meramente
documentação (``scoring_weights.yaml``, ``sources.yaml``) e não eram lidos por
nenhum código — o "paradoxo" resolvido pelo Bloco 2 (E2-T1) do SRA v7.0.

Regras de design (alinhadas ao CLAUDE.md):
  - Falha graciosa: arquivo ausente ou YAML inválido -> ``{}`` (os defaults
    hardcoded do consumidor permanecem válidos, sem exceção no boot).
  - Cache por processo (``lru_cache``): o disco é lido no máximo uma vez.
  - Sem novas dependências: ``yaml`` já é dependência do projeto.

Caminho de resolução: ``<raiz_do_projeto>/config`` relativo a este arquivo
(``src/config_loader.py`` -> sobe um nível para ``src/``, mais um para a raiz).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# <raiz_do_projeto>/config
CONFIG_DIR: Path = Path(__file__).resolve().parent.parent / "config"


def _safe_load_yaml(filename: str) -> dict[str, Any]:
    """Lê um YAML de ``config/`` com fallback gracioso.

    Args:
        filename: Nome do arquivo dentro de ``config/`` (ex: ``scoring_weights.yaml``).

    Returns:
        dict: Conteúdo do YAML como dicionário. ``{}`` se o arquivo não existir
        ou o parsing falhar (nunca levanta).
    """
    path = CONFIG_DIR / filename
    if not path.exists():
        logger.debug("Config %s ausente — usando defaults hardcoded.", filename)
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning(
                "Config %s não é um mapping YAML (tipo=%s) — ignorando.",
                filename,
                type(data).__name__,
            )
            return {}
        return data
    except Exception as exc:  # noqa: BLE001 - falha de parsing não pode quebrar o boot
        logger.warning("Falha ao carregar config %s: %s", filename, exc)
        return {}


@lru_cache(maxsize=None)
def load_scoring_weights() -> dict[str, Any]:
    """Carrega ``config/scoring_weights.yaml`` (com cache).

    Returns:
        dict: Conteúdo completo do YAML. ``{}`` se ausente/inválido, de modo que
        ``HybridRankerConfig`` cai nos pesos default.
    """
    return _safe_load_yaml("scoring_weights.yaml")


@lru_cache(maxsize=None)
def load_sources_config() -> dict[str, Any]:
    """Carrega ``config/sources.yaml`` (com cache) com a lista de fontes.

    Returns:
        dict: Conteúdo completo do YAML. ``{}`` se ausente/inválido, de modo que
        a ``SearcherFactory`` registra todas as fontes (comportamento atual).
    """
    return _safe_load_yaml("sources.yaml")


def get_source_enabled(source_id: str, default: bool = True) -> bool:
    """Retorna se uma fonte está habilitada em ``config/sources.yaml``.

    Lê a flag ``enabled`` da entrada ``sources.<source_id>``. Ausência do arquivo
    ou da entrada -> ``default`` (por padrão ``True``, preservando o
    comportamento atual em que tudo é instanciado).

    Args:
        source_id: Identificador da fonte (ex: ``github``, ``reddit``).
        default: Valor retornado se o arquivo/entrada não existir.

    Returns:
        bool: ``True`` se a fonte deve ser instanciada pela fábrica.
    """
    config = load_sources_config()
    source_def = config.get("sources", {}).get(source_id)
    if not isinstance(source_def, dict):
        return default
    return bool(source_def.get("enabled", default))
