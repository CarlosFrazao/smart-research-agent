import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

from src.search.base_searcher import BaseSearcher
from src.types import Domain, SearchResult
from src.utils.circuit_breaker import CircuitBreakerOpen, CircuitBreakerRegistry
from src.utils.http_client import HTTPClient
from src.utils.retry import RetryConfig, with_retry

logger = logging.getLogger(__name__)

_RETRY_CONFIG = RetryConfig(
    max_retries=3,
    base_delay=1.0,
    max_delay=10.0,
    exponential_base=2.0,
    jitter=True,
    retryable_exceptions=(Exception,),
)

# Palavras de parada (PT/EN) que poluem a busca do arXiv e atraem papers
# irrelevantes (ex.: física de altas energias) quando usadas numa query longa.
# Listadas já sem acento — a sanitização normaliza os tokens antes de comparar.
_STOPWORDS = frozenset(
    {
        "a",
        "o",
        "e",
        "de",
        "da",
        "do",
        "das",
        "dos",
        "em",
        "no",
        "na",
        "nos",
        "nas",
        "para",
        "por",
        "com",
        "sem",
        "que",
        "um",
        "uma",
        "uns",
        "umas",
        "no",
        "início",
        "inicio",
        "in",
        "on",
        "at",
        "the",
        "of",
        "to",
        "for",
        "with",
        "and",
        "or",
        "by",
        "as",
        "into",
        "from",
        "about",
        "recepcao",
        "solucoes",
        "tecnicas",
        "conflitos",
        "integrado",
        "integrada",
        "integrados",
        "tecnica",
        "solucao",
        "conflito",
    }
)

# Mapeia o domínio detectado pelo IntentAnalyzer para categorias do arXiv.
# Isso impede que tópicos de engenharia de software retornem cosmologia/física
# (astro-ph.CO) quando a ordenação por relevância cruza termos raros.
_DOMAIN_CATEGORY_MAP: dict[str, list[str]] = {
    Domain.DEV_TOOLS.value: ["cs.SE", "cs.PL", "cs.DC", "cs.SW", "cs.CR"],
    Domain.AI_ML.value: ["cs.AI", "cs.LG", "cs.CL", "cs.CV", "stat.ML"],
    Domain.AUTOMATION.value: ["cs.SY", "cs.DC", "cs.RO", "eess.SY"],
    Domain.INFRASTRUCTURE.value: ["cs.DC", "cs.NI", "cs.OS", "cs.SY"],
    Domain.OPEN_SOURCE.value: ["cs.SE", "cs.PL", "cs.DC"],
    Domain.SAAS_B2B.value: ["cs.SE", "cs.CY", "cs.HC"],
    Domain.GENERAL.value: [],
    Domain.UNIVERSAL.value: [],
    Domain.NEWS.value: [],
}


class ArxivSearcher(BaseSearcher):
    """Buscador especializado para coletar e estruturar resultados vindos do Arxiv."""

    def __init__(self, config: dict[str, Any], firecrawl_client=None):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        self.base_url = "http://export.arxiv.org/api/query"
        self.http = HTTPClient(timeout=self.timeout)
        self.firecrawl_client = firecrawl_client
        self.circuit = CircuitBreakerRegistry.get(
            "arxiv_api", failure_threshold=3, recovery_timeout=300
        )

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        """Realiza busca assíncrona por termos no arXiv.

        Aceita ``query`` como ``str`` ou como um objeto ``ExpandedQuery``
        (ex.: quando chamado pelo ``DebateOrchestrator``), extraindo o texto
        da query em ambos os casos para manter compatibilidade retroativa.

        Args:
            query (str | ExpandedQuery): Termo ou query de busca a ser pesquisada.
            **kwargs: Parâmetros de pesquisa adicionais específicos do buscador.
                Suporta ``domain`` (str) vindo do IntentAnalyzer para delimitar
                categorias do arXiv.

        Returns:
            list[SearchResult]: Lista contendo os resultados padronizados encontrados.
        """
        query = self._as_query_string(query)
        domain = kwargs.get("domain")
        if not hasattr(self, "circuit"):
            self.circuit = CircuitBreakerRegistry.get(
                "arxiv_api", failure_threshold=3, recovery_timeout=300
            )

        try:
            return await self.circuit.call(self._do_search, query, domain)
        except CircuitBreakerOpen as e:
            logger.warning(f"ArxivSearcher: {e}")
            return self.fallback(query)

    @staticmethod
    def _as_query_string(query: Any) -> str:
        """Normaliza o argumento ``query`` para ``str``.

        O ``DebateOrchestrator`` (e outros callers) passam um objeto
        ``ExpandedQuery`` em vez de ``str``. Extraímos ``eq.query`` quando
        aplicável; caso contrário usamos ``str(query)`` como fallback seguro.

        Args:
            query (Any): Query bruta (str ou ExpandedQuery).

        Returns:
            str: Texto da query pronto para sanitização/busca.
        """
        query_attr = getattr(query, "query", None)
        if isinstance(query_attr, str) and query_attr.strip():
            return query_attr
        return str(query)

    @with_retry(_RETRY_CONFIG)
    async def _do_search(
        self, query: str, domain: str | None = None
    ) -> list[SearchResult]:
        """Executa a chamada HTTP/API interna para pesquisar no arXiv sem tratamento de falhas.

        Constrói uma ``search_query`` do arXiv que (1) sanitiza a query em
        linguagem natural (remove stopwords PT/EN e a preamble narrativa),
        (2) concatena os termos técnicos remanescentes com ``AND`` e (3) quando
        há um domínio de engenharia de software conhecido, restringe a
        ``cat:`` às categorias CS apropriadas — evitando que ordenação por
        relevância devolva artigos de física/cosmologia para tópicos de web.

        Args:
            query (str): Termo de busca a ser pesquisado.
            domain (str | None): Domínio detectado pelo IntentAnalyzer
                (ex.: ``dev_tools``, ``ai_ml``). Usado para delimitar categorias.

        Returns:
            list[SearchResult]: Resultados brutos ou pré-processados da busca.
        """
        search_query = self._build_search_query(query, domain)
        params = {
            "search_query": search_query,
            "start": 0,
            "max_results": min(self.max_results, 50),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }

        try:
            data = await self.http.get(self.base_url, params=params)
            text = data.get("text", "")
            results = self._parse_xml(text)

            # Fallback para o Research Index do Firecrawl se resultados nativos < 3
            if len(results) < 3 and self.firecrawl_client:
                logger.info(
                    f"ArxivSearcher: apenas {len(results)} resultados nativos. "
                    f"Acionando Firecrawl Research Index para '{query}'..."
                )
                try:
                    ri_results = await self.firecrawl_client.search_research_index(
                        query, limit=10
                    )
                    seen_urls = {r.url for r in results}
                    for item in ri_results:
                        normalized = self._normalize_research_index_result(item)
                        if normalized.url and normalized.url not in seen_urls:
                            results.append(normalized)
                            seen_urls.add(normalized.url)
                except Exception as ri_err:
                    logger.warning(f"Firecrawl Research Index falhou: {ri_err}")

            return results
        except Exception as e:
            logger.error(f"Arxiv search erro: {e}")
            return self.fallback(query)

    def _sanitize_query(self, query: str) -> list[str]:
        """Extrai termos técnicos significativos de uma query em linguagem natural.

        Normaliza acentos (PT) antes de comparar com as stopwords e de montar
        os termos de busca, tokeniza versões ``X.Y`` e preserva tokens técnicos
        (ex.: ``React``, ``Next.js``, ``NestJS``, ``concurrency``). Retorna
        lista vazia se nada relevante sobrar, forçando o chamador a usar a
        query original como fallback.

        Args:
            query (str): Query bruta em linguagem natural.

        Returns:
            list[str]: Termos técnicos úteis para a busca no arXiv.
        """
        import unicodedata

        def _deaccent(text: str) -> str:
            return "".join(
                c
                for c in unicodedata.normalize("NFKD", text)
                if not unicodedata.combining(c)
            )

        # Mantém letras acentuadas, dígitos e pontos (para "Next.js", "React 19").
        tokens = re.findall(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9.]*", query.lower())
        kept: list[str] = []
        for raw in tokens:
            tok = _deaccent(raw)
            if tok in _STOPWORDS:
                continue
            # Descarta tokens curtos que não são siglas/versões (ex.: "js").
            is_version = bool(re.fullmatch(r"\d+\.\d+", tok))
            if len(tok) <= 2 and not is_version:
                continue
            # Descarta números puros pequenos (ex.: "19", "15", "10") — versões
            # avulsas sem contexto não agregam valor e poluem a busca.
            if tok.isdigit() and len(tok) <= 2:
                continue
            kept.append(tok)
        # Deduplica preservando ordem.
        seen: set[str] = set()
        deduped: list[str] = []
        for tok in kept:
            if tok not in seen:
                seen.add(tok)
                deduped.append(tok)
        return deduped

    def _build_search_query(self, query: str, domain: str | None) -> str:
        """Constrói a ``search_query`` do arXiv a partir da query e do domínio.

        Estratégia:
        - Sanitiza a query e usa os termos técnicos com ``abs:`` + ``AND``.
        - Se houver categorias mapeadas para o domínio, adiciona ``(cat:...)``
          via ``OR`` para restringir a Computer Science / áreas afins.

        Args:
            query (str): Query original em linguagem natural.
            domain (str | None): Domínio detectado (ex.: ``dev_tools``).

        Returns:
            str: Valor do parâmetro ``search_query`` compatível com a API arXiv.
        """
        terms = self._sanitize_query(query)
        # Fallback: se a sanitização esvaziar a query, usa a original truncada.
        if not terms:
            base = " ".join(query.split()[:8])
            terms = [base] if base else ["software"]

        # Busca nos campos abs/ti (abstract/título) — combina os termos com OR
        # para que qualquer termo técnico relevante seja considerado; a ordenação
        # por relevância do arXiv pondera os que casam múltiplos termos.
        term_expr = " OR ".join(f"abs:{t}" for t in terms)

        categories = _DOMAIN_CATEGORY_MAP.get(domain or "", [])
        if categories:
            cat_expr = " OR ".join(f"cat:{c}" for c in categories)
            # Restringe aos termos OU (dentro das categorias alvo OU qualquer lugar).
            return f"({term_expr}) AND ({cat_expr})"

        return term_expr

    def _parse_xml(self, xml_text: str) -> list[SearchResult]:
        results = []
        try:
            root = ET.fromstring(xml_text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}

            for entry in root.findall("atom:entry", ns):
                title = entry.find("atom:title", ns)
                summary = entry.find("atom:summary", ns)
                link = entry.find("atom:link[@rel='alternate']", ns)
                published = entry.find("atom:published", ns)
                authors = entry.findall("atom:author/atom:name", ns)
                category = entry.find("atom:category", ns)

                if title is not None and link is not None:
                    results.append(
                        SearchResult(
                            source="arxiv",
                            title=(title.text or "").strip(),
                            url=link.get("href", ""),
                            description=(
                                summary.text[:500]
                                if summary is not None and summary.text
                                else ""
                            ),
                            metrics={
                                "published": published.text
                                if published is not None
                                else "",
                                "authors": [a.text for a in authors if a.text],
                                "primary_category": (
                                    category.get("term", "")
                                    if category is not None
                                    else ""
                                ),
                            },
                            raw={},
                        )
                    )
        except Exception as e:
            logger.error(f"Erro ao parsear XML do Arxiv: {e}")
        return results

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto vindo do arXiv para a entidade padrão `SearchResult`.

        Args:
            raw_result (Any): O resultado bruto retornado pela API ou scraper.

        Returns:
            SearchResult: Objeto padronizado contendo os dados normalizados.
        """
        return SearchResult(
            source="arxiv",
            title=raw_result.get("title", ""),
            url=raw_result.get("url", ""),
            description=raw_result.get("description", ""),
            metrics={},
            raw=raw_result,
        )

    def _normalize_research_index_result(self, item: dict[str, Any]) -> SearchResult:
        """Converte resultado do Firecrawl Research Index para SearchResult."""
        return SearchResult(
            source="arxiv_research_index",
            title=item.get("title", ""),
            url=item.get("url", ""),
            description=item.get("description", "") or item.get("markdown", "")[:500],
            metrics={"source_index": "firecrawl_research"},
            raw=item,
        )
