"""Searcher de listas Awesome do GitHub via busca na API ou scraping direto de repositorios."""

import logging
import re
from typing import Any

from src.search.base_searcher import BaseSearcher
from src.search.github_searcher import GitHubSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)


class AwesomeSearcher(BaseSearcher):
    """Buscador especializado para coletar e estruturar resultados vindos do Awesome."""

    def __init__(self, config: dict[str, Any]):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        self.github = GitHubSearcher(config)
        self.http = HTTPClient(timeout=self.timeout)

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Realiza busca assíncrona por termos no Awesome.

        Args:
            query (str): Termo ou query de busca a ser pesquisada.
            **kwargs: Parâmetros de pesquisa adicionais específicos do buscador.

        Returns:
            list[SearchResult]: Lista contendo os resultados padronizados encontrados.
        """
        awesome_query = f"awesome-{query}"
        repos = await self.github.search(awesome_query)

        results = []
        for repo in repos[:3]:
            try:
                readme = await self._fetch_readme(repo.title)
                links = self._extract_links(readme)
                for link in links[:10]:
                    results.append(
                        SearchResult(
                            source="awesome",
                            title=link.get("title", "Link"),
                            url=link.get("url", ""),
                            description=link.get("description", ""),
                            metrics={
                                "awesome_list": repo.title,
                                "list_stars": repo.metrics.get("stars", 0),
                                "position": link.get("position", 0),
                            },
                            raw=link,
                        )
                    )
            except Exception as e:
                logger.warning(f"Erro ao processar awesome list {repo.title}: {e}")

        return results

    async def _fetch_readme(self, repo_name: str) -> str:
        for branch in ("main", "master"):
            url = f"https://raw.githubusercontent.com/{repo_name}/{branch}/README.md"
            try:
                data = await self.http.get(url)
                return data.get("text", "")
            except Exception:
                continue
        return ""

    def _extract_links(self, readme: str) -> list[dict]:
        links = []
        lines = readme.split("\n")

        for i, line in enumerate(lines):
            # Tenta capturar o padrão rico: - [Nome](URL) - Descrição
            match = re.search(
                r"^\s*[-*]\s*\[([^\]]+)\]\((https?://[^)]+)\)\s*[-—:]\s*(.+)$", line
            )
            if match:
                title, url, desc = match.groups()
                links.append(
                    {
                        "title": title.strip(),
                        "url": url.strip(),
                        "description": desc.strip(),
                        "position": i,
                    }
                )
            else:
                # Fallback para o padrão simples: [Nome](URL) e pega a descrição na linha seguinte
                match_simple = re.search(r"\[([^\]]+)\]\((https?://[^)]+)\)", line)
                if match_simple:
                    title, url = match_simple.groups()
                    next_desc = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    if next_desc and not next_desc.startswith(("-", "*", "[", "#")):
                        desc = next_desc[:200]
                    else:
                        desc = ""
                    links.append(
                        {
                            "title": title.strip(),
                            "url": url.strip(),
                            "description": desc,
                            "position": i,
                        }
                    )
        return links

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto vindo do Awesome para a entidade padrão `SearchResult`.

        Args:
            raw_result (Any): O resultado bruto retornado pela API ou scraper.

        Returns:
            SearchResult: Objeto padronizado contendo os dados normalizados.
        """
        return SearchResult(
            source="awesome",
            title=raw_result.get("title", ""),
            url=raw_result.get("url", ""),
            description=raw_result.get("description", ""),
            metrics={},
            raw=raw_result,
        )
