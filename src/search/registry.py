"""Registro central de searchers via decorator @register_searcher.

Permite que novos searchers se registrem automaticamente sem modificar factory.py.
"""

from __future__ import annotations
import logging
from typing import Callable, Type

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, dict] = {}


def register_searcher(
    name: str,
    *,
    requires_key: str | None = None,
    enabled_env: str | None = None,
    trusted: bool = True,
) -> Callable:
    """Decorator para auto-registro de searchers.

    Args:
        name: Identificador único (ex: "wikipedia").
        requires_key: Env var obrigatória com a API key (ex: "SERP_API_KEY").
        enabled_env: Env var para ativar/desativar (ex: "SRA_WIKIPEDIA_ENABLED").
        trusted: Se True, fonte isenta de sanitização LLM. Default True.

    Exemplo:
        @register_searcher("wikipedia", enabled_env="SRA_WIKIPEDIA_ENABLED", trusted=True)
        class WikipediaSearcher(APISearcher):
            ...
    """

    def decorator(cls: Type) -> Type:
        _REGISTRY[name] = {
            "cls": cls,
            "requires_key": requires_key,
            "enabled_env": enabled_env,
            "trusted": trusted,
        }
        logger.debug("Searcher '%s' registrado via @register_searcher", name)
        return cls

    return decorator


def get_registry() -> dict[str, dict]:
    """Retorna cópia do registro atual."""
    return dict(_REGISTRY)


def list_registered() -> list[str]:
    """Lista nomes de searchers registrados."""
    return list(_REGISTRY.keys())
