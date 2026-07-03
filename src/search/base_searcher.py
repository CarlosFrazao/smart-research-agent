import logging
from abc import ABC, abstractmethod
from typing import Any

from src.types import SearchResult

logger = logging.getLogger(__name__)


class BaseSearcher(ABC):
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.timeout = config.get("timeout", 30)
        self.max_results = config.get("max_results", 20)
        self.enabled = config.get("enabled", True)

    @abstractmethod
    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        pass

    @abstractmethod
    def normalize(self, raw_result: Any) -> SearchResult:
        pass

    def fallback(self, query: str) -> list[SearchResult]:
        logger.warning(f"Fallback ativado para {self.__class__.__name__}: {query[:50]}")
        return []
