"""QuoraSearcher — Busca no Quora via cascata de scrapers resilientes.

O Quora não dispõe de API pública de busca, portanto este searcher utiliza a
cascata ``ScrapingSearcher`` (Firecrawl -> Spider -> Steel -> Jina) para obter
o HTML da página de resultados e extrair perguntas e snippets de resposta.

URL de busca: ``https://www.quora.com/search?q={query}``

Como o Quora aplica detecção anti-bot agressiva (403 / CAPTCHA), qualquer falha
na cascata de scraping é tratada com degradação graciosa: retorna lista vazia
sem lançar exceção. O mesmo acontece se nenhum scraper estiver disponível.

A fonte é marcada como não-confiável (``trusted=False``) — passa pelo
``LLMSanitizer`` em ``search_stage.py``.
"""

from __future__ import annotations

import html
import logging
import re
import urllib.parse
from typing import Any

from src.search.registry import register_searcher
from src.search.scraping_searcher import ScrapingError, ScrapingSearcher
from src.types import SearchResult

logger = logging.getLogger("search.quora")

QUORA_SEARCH_URL = "https://www.quora.com/search"
# Padrões típicos de URL de pergunta do Quora
_QUORA_QUESTION_RE = re.compile(
    r"/(?:topic/|profile/|What-|How-|Why-|Is-|Are-|Should-|Can-|When-|Where-|Who-|Which-)",
    re.IGNORECASE,
)
# Extrai pares <a href="...">texto</a> (tolerante a atributos)
_ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@register_searcher("quora", enabled_env="SRA_QUORA_ENABLED", trusted=False)
class QuoraSearcher(ScrapingSearcher):
    """Searcher para Quora via cascata de scraping.

    Herda ``ScrapingSearcher`` para aproveitar a cascata Firecrawl->Spider->
    Steel->Jina. Constrói a URL de busca, scrapeia o HTML e extrai perguntas
    e snippets de resposta.

    Args:
        config: Dict de configuração (aceita ``timeout`` — padrão 15s — e as
            chaves usuais de ``ScrapingConfig``).
        scrapers: Mapa opcional {nome: instância} de scrapers. Se ausente, a
            busca degrada graciosamente para lista vazia.
    """

    def __init__(
        self,
        config: dict[str, Any],
        scrapers: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        # Timeout mais curto: scraping do Quora é lento e sujeito a bloqueio
        config = dict(config)
        config.setdefault("timeout", 15.0)
        super().__init__(config, scrapers=scrapers, **kwargs)
        self._source_name = "quora"
        self._min_snippet_len = int(config.get("min_snippet_len", 30))

    @property
    def source_name(self) -> str:
        """Identificador da fonte (usado pelo pipeline e pelos testes)."""
        return self._source_name

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Executa busca no Quora via scraping da página de resultados.

        Args:
            query: Termo de busca.
            **kwargs: Parâmetros ignorados.

        Returns:
            Lista de SearchResult. Vazia em caso de bloqueio/erro (degradação
            graciosa).
        """
        url = f"{QUORA_SEARCH_URL}?q={urllib.parse.quote_plus(query)}"
        try:
            scraped = await self._scrape_url(url)
        except ScrapingError as e:
            logger.warning(
                f"QuoraSearcher: cascata de scraping falhou para '{query}': {e}"
            )
            return []
        except Exception as e:
            logger.warning(f"QuoraSearcher: erro de scraping para '{query}': {e}")
            return []

        content = scraped.get("markdown") or scraped.get("content") or ""
        if not content or len(content.strip()) < self._min_snippet_len:
            # HTML bloqueado / CAPTCHA / resposta vazia — degradação graciosa
            logger.debug(f"QuoraSearcher: conteúdo insuficiente para '{query}'")
            return []

        results = self._parse_html(content, url)
        logger.debug(f"QuoraSearcher: {len(results)} resultados para '{query}'")
        return results[: self.max_results]

    def _parse_html(self, html_content: str, base_url: str) -> list[SearchResult]:
        """Extrai perguntas e snippets de resposta do HTML do Quora.

        Estratégia de baixa dependência (apenas stdlib):
          1. Percorre âncoras ``<a>`` cujo href aponta para pergunta/tópico do
             Quora — usando o texto do link como título.
          2. Para cada âncora relevante, captura o snippet de texto seguinte
             (até o próximo marcador de bloco) como descrição.
          3. Fallback: se nenhuma âncora for encontrada, faz strip de tags e
             usa o texto como descrição de um único resultado.

        Args:
            html_content: HTML (ou markdown derivado) retornado pelo scraper.
            base_url: URL de busca original (usada como fallback de url).

        Returns:
            Lista de SearchResult.
        """
        results: list[SearchResult] = []
        seen_urls: set[str] = set()

        for href, inner in _ANCHOR_RE.findall(html_content):
            if not _QUORA_QUESTION_RE.search(href or ""):
                continue
            abs_url = (
                href
                if href.startswith("http")
                else urllib.parse.urljoin(base_url, href)
            )
            if abs_url in seen_urls:
                continue
            title = _WS_RE.sub(" ", _strip_tags(inner)).strip()
            title = html.unescape(title)
            if not title:
                continue
            # Snippet: pega o texto logo após o fechamento do <a> até ~300 chars
            snippet = _extract_snippet(html_content, href, inner)
            results.append(
                SearchResult(
                    source="quora",
                    title=title[:200],
                    url=abs_url,
                    description=snippet,
                    metrics={"source": "quora_search"},
                )
            )
            seen_urls.add(abs_url)

        if not results:
            # Fallback: sem âncoras reconhecíveis — usa o texto cru
            text = _WS_RE.sub(" ", _strip_tags(html_content)).strip()
            text = html.unescape(text)
            if len(text) >= self._min_snippet_len:
                results.append(
                    SearchResult(
                        source="quora",
                        title=text[:120],
                        url=base_url,
                        description=text[:600],
                        metrics={"source": "quora_search"},
                    )
                )

        return results

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto em SearchResult."""
        if isinstance(raw_result, SearchResult):
            return raw_result
        if isinstance(raw_result, dict):
            return SearchResult(
                source="quora",
                title=raw_result.get("title", "Resultado Quora"),
                url=raw_result.get("url", ""),
                description=raw_result.get("description", ""),
            )
        return SearchResult(
            source="quora",
            title="Resultado Quora",
            url="",
            description=str(raw_result),
        )


def _strip_tags(text: str) -> str:
    """Remove tags HTML de um fragmento de texto."""
    return _TAG_RE.sub("", text)


def _extract_snippet(html_content: str, href: str, inner: str) -> str:
    """Extrai o snippet de texto seguinte a uma âncora do Quora.

    Localiza o fechamento ``</a>`` correspondente à âncora e captura o texto
    até o próximo marcador de bloco (``<`` ou cerca de 300 caracteres).
    """
    anchor_full = f'href="{href}"' if f'href="{href}"' in html_content else href
    idx = html_content.find(anchor_full)
    if idx == -1:
        idx = html_content.find(inner)
    if idx == -1:
        return ""
    close_idx = html_content.find("</a>", idx)
    if close_idx == -1:
        close_idx = idx + len(inner)
    after = html_content[close_idx + len("</a>") : close_idx + len("</a>") + 400]
    snippet = _WS_RE.sub(" ", _strip_tags(after)).strip()
    return html.unescape(snippet)[:300]
