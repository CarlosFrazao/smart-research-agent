from typing import List, Dict, Any
from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient
import logging

logger = logging.getLogger(__name__)

DOMAIN_QUALIFIERS = {
    "saas_b2b": "stars:>50 sort:stars",
    "dev_tools": "stars:>100 sort:updated",
    "ai_ml": "stars:>200 sort:stars",
    "automation": "stars:>30 sort:stars",
    "infrastructure": "stars:>100 sort:updated",
    "open_source": "stars:>10 sort:stars",
    "general": "sort:stars",
}


class GitHubSearcher(BaseSearcher):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.token = config.get("github_token")
        self.base_url = "https://api.github.com/search/repositories"
        self.http = HTTPClient(timeout=self.timeout)

    async def search(self, query: str, domain: str = "general", **kwargs) -> List[SearchResult]:
        # 1. Clean query: remove stop words and keep it concise for GitHub search
        stop_words = {"for", "with", "and", "or", "in", "to", "best", "alternatives", "alternative", "solutions", "solution", "of", "the", "a", "an", "on", "using", "by", "from", "how"}
        words = [w for w in query.replace("-", " ").split() if w.lower() not in stop_words]
        
        # If still too long, keep only the most significant terms
        if len(words) > 4:
            words = words[:4]
        
        cleaned_query = " ".join(words)
        if not cleaned_query:
            cleaned_query = query
            
        qualifiers = DOMAIN_QUALIFIERS.get(domain, "sort:stars")
        full_query = f"{cleaned_query} {qualifiers}"

        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "SmartResearchAgent/1.0",
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"

        params = {
            "q": full_query,
            "sort": "stars",
            "order": "desc",
            "per_page": min(self.max_results, 100),
        }

        try:
            logger.info(f"GitHub buscando: '{full_query}'")
            data = await self.http.get(self.base_url, headers=headers, params=params)
            items = data.get("items", [])
            
            # 2. Resilient Fallback: if rigid qualifiers returned 0 results, retry with a cleaner search
            if not items and qualifiers != "sort:stars":
                fallback_query = f"{cleaned_query} sort:stars"
                logger.info(f"GitHub 0 resultados. Tentando fallback mais brando: '{fallback_query}'")
                params["q"] = fallback_query
                data = await self.http.get(self.base_url, headers=headers, params=params)
                items = data.get("items", [])
                
            results = [self.normalize(item) for item in items]

            # 3. Code Search fallback: acionar busca por conteúdo se repos < 2
            if len(results) < 2:
                logger.info(f"GitHub repos < 2 para '{query}'. Ativando Code Search...")
                code_results = await self.search_code(query)
                seen_urls = {r.url for r in results}
                for r in code_results:
                    if r.url not in seen_urls:
                        results.append(r)
                        seen_urls.add(r.url)

            return results
        except Exception as e:
            logger.error(f"GitHub search erro: {e}")
            return self.fallback(query)

    async def search_code(self, query: str, language: str = None) -> List[SearchResult]:
        """Code Search via GitHub API — busca conteúdo dentro de arquivos."""
        code_search_url = "https://api.github.com/search/code"
        q = f"{query} language:{language}" if language else query

        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "SmartResearchAgent/1.0",
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"

        params = {
            "q": q,
            "per_page": min(self.max_results, 30),
        }

        try:
            data = await self.http.get(code_search_url, headers=headers, params=params)
            items = data.get("items", [])
            return [
                SearchResult(
                    source="github_code",
                    title=item.get("name", ""),
                    url=item.get("html_url", ""),
                    description=(
                        f"Arquivo em {item.get('repository', {}).get('full_name', '')} "
                        f"— path: {item.get('path', '')}"
                    ),
                    metrics={
                        "repo": item.get("repository", {}).get("full_name", ""),
                        "path": item.get("path", ""),
                        "sha": item.get("sha", ""),
                    },
                    raw=item,
                )
                for item in items
            ]
        except Exception as e:
            logger.warning(f"GitHub Code Search erro: {e}")
            return []

    def normalize(self, item: dict) -> SearchResult:
        updated_at = item.get("pushed_at", item.get("updated_at", ""))
        license_info = item.get("license") or {}

        return SearchResult(
            source="github",
            title=item.get("full_name", ""),
            url=item.get("html_url", ""),
            description=item.get("description", "") or "",
            metrics={
                "stars": item.get("stargazers_count", 0),
                "forks": item.get("forks_count", 0),
                "open_issues": item.get("open_issues_count", 0),
                "language": item.get("language"),
                "updated_at": updated_at,
                "created_at": item.get("created_at", ""),
                "license": license_info.get("spdx_id") if license_info else None,
                "topics": item.get("topics", []),
                "watchers": item.get("watchers_count", 0),
            },
            raw=item,
        )
