import logging
from typing import Any

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
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

DOMAIN_QUALIFIERS = {
    "saas_b2b": "stars:>50 sort:stars",
    "dev_tools": "stars:>100 sort:updated",
    "ai_ml": "stars:>200 sort:stars",
    "automation": "stars:>30 sort:stars",
    "infrastructure": "stars:>100 sort:updated",
    "open_source": "stars:>10 sort:stars",
    "general": "sort:stars",
}

# ── Melhoria B+: busca enriquecida de issues/PRs ──────────────────────────────
# Termos técnicos em inglês preservados ao extrair a query de issues.
_ISSUE_TECH_TERMS_EN = {
    "lock",
    "locks",
    "mutex",
    "semaphore",
    "deadlock",
    "starvation",
    "race",
    "concurrency",
    "parallel",
    "thread",
    "threads",
    "process",
    "processes",
    "reader",
    "readers",
    "writer",
    "writers",
    "read",
    "write",
    "atomic",
    "atomicity",
    "fcntl",
    "msvcrt",
    "native",
    "windows",
    "linux",
    "macos",
    "python",
    "rust",
    "go",
    "cpp",
    "c++",
    "java",
    "async",
    "await",
    "io",
    "file",
    "files",
    "condition",
    "variable",
    "synchronization",
    "sync",
    "scheduler",
    "queue",
    "buffer",
    "cache",
    "memory",
    "cpu",
    "gpu",
}

# Mapeamento português -> inglês de termos técnicos de concorrência/sistemas.
_ISSUE_TERM_PT_TO_EN = {
    "concorrencia": "concurrency",
    "concurrencia": "concurrency",
    "concorrência": "concurrency",
    "escritor": "writer",
    "escritores": "writer",
    "escritora": "writer",
    "escritoras": "writer",
    "escrita": "write",
    "leitor": "reader",
    "leitores": "reader",
    "leitora": "reader",
    "leitoras": "reader",
    "leitura": "read",
    "bloqueio": "lock",
    "trava": "lock",
    "travamento": "deadlock",
    "inanição": "starvation",
    "inanicao": "starvation",
    "esfome": "starvation",
    "esfomeação": "starvation",
    "mutex": "mutex",
    "semáforo": "semaphore",
    "semaforo": "semaphore",
    "atômico": "atomic",
    "atomico": "atomic",
    "thread": "thread",
    "processo": "process",
    "nativo": "native",
    "janela": "windows",
    "linux": "linux",
    "resolver": "resolve",
    "comportamento": "behavior",
    "mapear": "map",
    "paralelo": "parallel",
    "sincronização": "synchronization",
    "sincronizacao": "synchronization",
}

# Stop words para a extração de termos técnicos de issues (PT + EN).
_ISSUE_STOP_WORDS = {
    "de",
    "da",
    "do",
    "das",
    "dos",
    "o",
    "a",
    "os",
    "as",
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
    "e",
    "ou",
    "um",
    "uma",
    "uns",
    "umas",
    "to",
    "the",
    "for",
    "with",
    "and",
    "or",
    "in",
    "on",
    "using",
    "by",
    "from",
    "how",
    "which",
    "of",
    "is",
    "are",
    "was",
    "were",
    "be",
    "analise",
    "analisar",
    "mapeie",
    "identifique",
    "compare",
    "comparing",
    "resolve",
    "resolver",
    "design",
    "ocorridas",
    "entre",
    "sobre",
    "como",
    "apply",
    "aplicar",
    "own",
    "proprio",
    "discuss",
    "discusses",
    "discussions",
    "issues",
    "changes",
    "alteracoes",
    "learn",
    "lessons",
    "licoes",
    "learned",
    "aprendidas",
    "final",
    "behavior",
    "map",
}

ISSUES_SEARCH_URL = "https://api.github.com/search/issues"


class GitHubSearcher(BaseSearcher):
    """Buscador especializado para coletar e estruturar resultados vindos do GitHub."""

    def __init__(self, config: dict[str, Any]):
        """Inicializa o buscador com configurações e clientes necessários.

        Args:
            config (dict[str, Any]): Dicionário contendo as configurações globais do agente.
        """
        super().__init__(config)
        self.token = config.get("github_token")
        self.base_url = "https://api.github.com/search/repositories"
        self.http = HTTPClient(timeout=self.timeout)
        self.circuit = CircuitBreakerRegistry.get(
            "github_api", failure_threshold=3, recovery_timeout=600
        )

    def _extract_semantic_filters(self, query: str) -> dict[str, str]:
        """Extrai filtros estruturados de linguagem, data, estrelas e tópicos da query natural."""
        filters = {}
        query_lower = query.lower()
        import re
        from datetime import datetime, timedelta

        # 1. Extração de Linguagem
        lang_match = re.search(
            r"\b(rust|python|go|golang|typescript|javascript|java|kotlin|swift|c\+\+|cpp)\b",
            query_lower,
        )
        if lang_match:
            lang = lang_match.group(1)
            if lang == "golang":
                lang = "go"
            filters["language"] = lang

        # 2. Extração de Data/Recência (last N days/weeks/months)
        date_match = re.search(r"last\s+(\d+)\s+(day|week|month)s?", query_lower)
        if date_match:
            amount = int(date_match.group(1))
            unit = date_match.group(2)
            now = datetime.now()
            if unit == "day":
                delta = timedelta(days=amount)
            elif unit == "week":
                delta = timedelta(weeks=amount)
            elif unit == "month":
                delta = timedelta(days=amount * 30)
            else:
                delta = timedelta(days=30)
            cutoff_date = (now - delta).strftime("%Y-%m-%d")
            filters["created"] = f">{cutoff_date}"

        # 3. Extração de Estrelas (more than N stars ou >N stars ou at least N stars)
        stars_match = re.search(
            r"(?:more than|>\s*|at least\s*|above\s*)\s*(\d+)\s*stars?", query_lower
        )
        if stars_match:
            filters["stars"] = f">{stars_match.group(1)}"

        # 4. Extração de Tópicos comuns
        topics = ["mcp", "llm", "ai", "fastapi", "docker", "cli"]
        for topic in topics:
            if topic in query_lower:
                filters["topic"] = topic
                break

        return filters

    async def search(
        self, query: str, domain: str = "general", **kwargs
    ) -> list[SearchResult]:
        """Realiza busca assíncrona por termos no Github.

        Args:
            query (str): Termo ou query de busca a ser pesquisada.
            **kwargs: Parâmetros de pesquisa adicionais específicos do buscador.

        Returns:
            list[SearchResult]: Lista contendo os resultados padronizados encontrados.
        """
        query_lower = query.lower()
        # 1. Clean query: remove stop words and keep it concise for GitHub search
        stop_words = {
            "for",
            "with",
            "and",
            "or",
            "in",
            "to",
            "best",
            "alternatives",
            "alternative",
            "solutions",
            "solution",
            "of",
            "the",
            "a",
            "an",
            "on",
            "using",
            "by",
            "from",
            "how",
            "which",
            "repositories",
            "repository",
            "implementing",
            "implemented",
        }
        words = [
            w for w in query.replace("-", " ").split() if w.lower() not in stop_words
        ]

        # If still too long, keep only the most significant terms (aumentado para 8)
        if len(words) > 8:
            words = words[:8]

        cleaned_query = " ".join(words)
        if not cleaned_query:
            cleaned_query = query

        # Extração de filtros semânticos
        filters = self._extract_semantic_filters(query)
        filter_str = " ".join(f"{k}:{v}" for k, v in filters.items())

        qualifiers = DOMAIN_QUALIFIERS.get(domain, "sort:stars")

        # Composição final da query semântica
        if filter_str:
            full_query = f"{cleaned_query} {filter_str} {qualifiers}"
        else:
            full_query = f"{cleaned_query} {qualifiers}"

        # Limpar múltiplos espaços
        import re

        full_query = re.sub(r"\s+", " ", full_query).strip()

        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "SmartResearchAgent/1.0",
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"

        # Determinar sort dinâmico
        sort_by = "stars"
        if "created" in filters or "recent" in query_lower or "last" in query_lower:
            sort_by = "updated"

        params = {
            "q": full_query,
            "sort": sort_by,
            "order": "desc",
            "per_page": min(self.max_results, 100),
        }

        if not hasattr(self, "circuit"):
            self.circuit = CircuitBreakerRegistry.get(
                "github_api", failure_threshold=3, recovery_timeout=600
            )

        try:
            logger.info(f"GitHub buscando: '{full_query}'")
            return await self.circuit.call(
                self._do_search,
                full_query,
                cleaned_query,
                qualifiers,
                query,
                headers,
                params,
            )
        except CircuitBreakerOpen as e:
            logger.warning(f"GitHubSearcher: {e}")
            return self.fallback(query)
        except Exception as e:
            logger.error(f"GitHub search erro: {e}")
            return self.fallback(query)

    @with_retry(_RETRY_CONFIG)
    async def _do_search(
        self,
        full_query: str,
        cleaned_query: str,
        qualifiers: str,
        original_query: str,
        headers: dict,
        params: dict,
    ) -> list[SearchResult]:
        """Lógica real de busca no GitHub API, protegida pelo circuit breaker."""
        # Guarda a query original para extração de termos técnicos no fallback de issues
        self._last_query = original_query

        data = await self.http.get(self.base_url, headers=headers, params=params)
        items = data.get("items", [])

        # 2. Resilient Fallback: se qualificadores rígidos retornaram 0 resultados
        if not items and qualifiers != "sort:stars":
            fallback_query = f"{cleaned_query} sort:stars"
            logger.info(
                f"GitHub 0 resultados. Tentando fallback mais brando: '{fallback_query}'"
            )
            params["q"] = fallback_query
            data = await self.http.get(self.base_url, headers=headers, params=params)
            items = data.get("items", [])

        results = [self.normalize(item) for item in items]

        # 3. Code Search fallback: se repos < 2
        if len(results) < 2:
            logger.info(
                f"GitHub repos < 2 para '{original_query}'. Ativando Code Search..."
            )
            code_results = await self.search_code(original_query)
            seen_urls = {r.url for r in results}
            for r in code_results:
                if r.url not in seen_urls:
                    results.append(r)
                    seen_urls.add(r.url)

        # 4. Issues/PRs fallback (Melhoria B+): se 0 resultados relevantes e a
        #    query cita um repositório específico, busca discussões técnicas em
        #    inglês na API de issues — contornando o limite do Code Search.
        if not results:
            repo = self._extract_repo_from_query(original_query)
            if repo:
                logger.info(
                    f"GitHub 0 resultados para '{original_query}'. "
                    f"Ativando busca de issues/PRs para '{repo}'..."
                )
                issue_results = await self.search_repo_issues(repo)
                seen_urls = {r.url for r in results}
                for r in issue_results:
                    if r.url not in seen_urls:
                        results.append(r)
                        seen_urls.add(r.url)

        return results

    def _normalize_code_query(self, query: str) -> str:
        """Normaliza uma query livre (ex.: pergunta em português) para a API de
        Code Search do GitHub.

        A API de code search é sensível a caracteres não-ASCII, parênteses,
        aspas e a queries muito longas — retornando 422 (Unprocessable Entity)
        quando recebe a pergunta bruta do usuário. Esta função:
          1. Remove acentos (NFKD) e caracteres não-ASCII.
          2. Remove pontuação/parênteses.
          3. Descarta stop words e limita a ~8 termos técnicos.
          4. Trunca o resultado em ~100 caracteres (limite prático da API).

        Returns:
            str: Query ASCII, minúscula e válida para ``api.github.com/search/code``.
        """
        import re
        import unicodedata

        # 1. Remover acentos e normalizar para ASCII
        ascii_query = (
            unicodedata.normalize("NFKD", query)
            .encode("ascii", "ignore")
            .decode("ascii")
        )

        # 2. Remover pontuação, parênteses e múltiplos espaços
        ascii_query = re.sub(r"[^\w\s]", " ", ascii_query)
        ascii_query = re.sub(r"\s+", " ", ascii_query).strip()

        # 3. Filtrar stop words e manter apenas termos técnicos relevantes
        stop_words = {
            "de",
            "do",
            "da",
            "dos",
            "das",
            "o",
            "a",
            "os",
            "as",
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
            "e",
            "ou",
            "um",
            "uma",
            "uns",
            "umas",
            "to",
            "the",
            "for",
            "with",
            "and",
            "or",
            "in",
            "on",
            "using",
            "by",
            "from",
            "how",
            "which",
            "of",
            "is",
            "are",
            "was",
            "were",
            "be",
            "analise",
            "analyse",
            "analisar",
            "mapeie",
            "mapear",
            "identifique",
            "identify",
            "compare",
            "comparing",
            "resolve",
            "resolver",
            "design",
            "occurrences",
            "ocorridas",
            "ocorridas",
            "entre",
            "between",
            "final",
            "sobre",
            "about",
            "como",
            "comportamento",
            "behavior",
            "apply",
            "aplicar",
            "own",
            "proprio",
            "proprio",
            "discussions",
            "discusses",
            "discuss",
            "issues",
            "changes",
            "alteracoes",
            "alteracoes",
            "learn",
            "lessons",
            "licoes",
            "learned",
            "aprendidas",
        }
        words = [w for w in ascii_query.lower().split() if w not in stop_words]

        # 4. Limitar a 8 termos mais significativos (evita 422 por query longa)
        if len(words) > 8:
            words = words[:8]

        cleaned = " ".join(words)
        if not cleaned:
            cleaned = ascii_query[:50]

        # 5. Truncar para respeitar limite prático da API de code search
        return cleaned[:100].strip()

    def _extract_repo_from_query(self, query: str) -> str | None:
        """Extrai um ``owner/repo`` citado na query do usuário.

        Reconhece tanto a forma completa ``github.com/owner/repo`` quanto a
        abreviada ``owner/repo`` (ex.: ``kuzudb/kuzu``). Usa a versão ASCII
        da query (sem acentos) para tolerar perguntas em português.

        Returns:
            str | None: ``"owner/repo"`` se encontrado, senão ``None``.
        """
        import re
        import unicodedata

        ascii_query = (
            unicodedata.normalize("NFKD", query)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        # 1. Forma completa: github.com/owner/repo
        m = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", ascii_query)
        if m:
            return m.group(1)
        # 2. Forma abreviada: owner/repo (evita casar palavras soltas com '/')
        m = re.search(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b", ascii_query)
        if m:
            candidate = m.group(1)
            # Rejeita caminhos de sistema (ex.: kuzu_data/kuzu.db) ou URLs
            if any(sep in candidate for sep in ("\\", "://", "./", "../")):
                return None
            return candidate
        return None

    def _build_code_query(self, query: str) -> str:
        """Constrói a query final de Code Search do GitHub.

        Se a query cita um repositório (``github.com/owner/repo`` ou
        ``owner/repo``), direciona a busca com o qualifier ``repo:owner/repo``
        + termos técnicos extraídos — o que retorna resultados reais do repo
        (code search indexa código, não texto em português).

        Caso contrário, cai na normalização genérica (ASCII, sem acentos).

        Args:
            query (str): Pergunta/original do usuário (podendo ser em português).

        Returns:
            str: Query válida para ``api.github.com/search/code``.
        """
        repo = self._extract_repo_from_query(query)
        if repo:
            normalized = self._normalize_code_query(query)
            # Remove o próprio "owner/repo" da lista de termos para não duplicar
            terms = " ".join(
                w for w in normalized.split() if w != repo.replace("/", "")
            )
            terms = terms.strip()
            if terms:
                return f"repo:{repo} {terms}"
            return f"repo:{repo}"
        return self._normalize_code_query(query)

    def _extract_technical_terms(self, query: str) -> str:
        """Extrai e traduz termos técnicos de uma query (PT/EN) para inglês.

        Usado pela busca enriquecida de issues/PRs: a API de issues do GitHub
        indexa texto em inglês, então termos em português são mapeados para a
        forma em inglês equivalente (ex.: ``concorrência`` -> ``concurrency``,
        ``escritor`` -> ``writer``, ``inanição`` -> ``starvation``). Termos já
        em inglês na whitelist de tecnologia são preservados.

        Args:
            query (str): Pergunta do usuário (podendo ser em português).

        Returns:
            str: Termos técnicos relevantes separados por espaço (ASCII), prontos
            para compor a query de issues. String vazia se nada relevante.
        """
        import re
        import unicodedata

        # 1. Normalizar para ASCII (remove acentos)
        ascii_query = (
            unicodedata.normalize("NFKD", query)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        words = [w for w in re.sub(r"[^\w\s]", " ", ascii_query).lower().split()]

        terms: list[str] = []
        for w in words:
            if w in _ISSUE_STOP_WORDS:
                continue
            # Termo em inglês conhecido na whitelist técnica
            if w in _ISSUE_TECH_TERMS_EN:
                if w not in terms:
                    terms.append(w)
                continue
            # Mapeamento PT -> EN
            mapped = _ISSUE_TERM_PT_TO_EN.get(w)
            if mapped and mapped not in terms:
                terms.append(mapped)

        return " ".join(terms)

    def _build_issues_query(self, repo: str, terms: str) -> str:
        """Formata a query de busca de issues/PRs no padrão do GitHub.

        Args:
            repo (str): ``owner/repo`` citado (ex.: ``kuzudb/kuzu``).
            terms (str): Termos técnicos extraídos/traduzidos por
                ``_extract_technical_terms``.

        Returns:
            str: Query no formato ``repo:owner/repo is:issue <termos>``
            (ou ``repo:owner/repo is:issue`` quando ``terms`` está vazio).
        """
        terms = (terms or "").strip()
        if terms:
            return f"repo:{repo} is:issue {terms}"
        return f"repo:{repo} is:issue"

    def _normalize_issue_list(self, items: list[dict[str, Any]]) -> list[SearchResult]:
        """Normaliza a lista bruta de issues/PRs da API para ``SearchResult``.

        Args:
            items (list[dict]): Lista de itens ``items`` da resposta de
                ``/search/issues``.

        Returns:
            list[SearchResult]: Resultados normalizados com ``source``
            ``"github_issues"``.
        """
        results: list[SearchResult] = []
        for item in items:
            is_pr = bool(item.get("pull_request"))
            body = item.get("body") or ""
            description = (body[:280] + "…") if len(body) > 280 else body
            results.append(
                SearchResult(
                    source="github_issues",
                    title=f"#{item.get('number')} {item.get('title', '')}",
                    url=item.get("html_url", ""),
                    description=description,
                    metrics={
                        "number": item.get("number"),
                        "state": item.get("state"),
                        "is_pull_request": is_pr,
                        "comments": item.get("comments", 0),
                        "user": (item.get("user") or {}).get("login"),
                        "created_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                        "labels": [
                            (lbl.get("name") if isinstance(lbl, dict) else lbl)
                            for lbl in (item.get("labels") or [])
                        ],
                    },
                    raw=item,
                )
            )
        return results

    async def search_repo_issues(self, repo: str) -> list[SearchResult]:
        """Busca issues/PRs técnicos do repositório citado via GitHub Search API.

        Contorna o limite do Code Search (que indexa apenas código-fonte e falha
        com texto em português) consultando ``/search/issues`` usando o qualifier
        ``repo:owner/repo is:issue <termos>``. Os termos técnicos são extraídos
        pela query original do usuário via ``_extract_technical_terms``.

        Args:
            repo (str): ``owner/repo`` citado na query (ex.: ``kuzudb/kuzu``).

        Returns:
            list[SearchResult]: Issues/PRs normalizados (``source``
            ``"github_issues"``). Lista vazia em caso de erro ou 0 resultados
            (degradação graciosa — não quebra o pipeline de busca).
        """
        if not repo or "/" not in repo:
            return []

        # Termos vêm da query original do usuário (guardada no último search)
        base_query = getattr(self, "_last_query", "") or ""
        terms = self._extract_technical_terms(base_query)
        q = self._build_issues_query(repo, terms)

        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "SmartResearchAgent/1.0",
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"

        params = {
            "q": q,
            "sort": "updated",
            "order": "desc",
            "per_page": min(self.max_results, 30),
        }

        try:
            data = await self.http.get(
                ISSUES_SEARCH_URL, headers=headers, params=params
            )
            items = data.get("items", []) if isinstance(data, dict) else []
            if not items:
                logger.info(f"GitHub issues: 0 resultados para '{q}'")
                return []
            logger.info(f"GitHub issues: {len(items)} issue(s)/PR(s) para '{q}'")
            return self._normalize_issue_list(items)
        except Exception as e:
            logger.warning(f"GitHub issues search erro: {e}")
            return []

    async def search_code(
        self, query: str, language: str | None = None
    ) -> list[SearchResult]:
        """Code Search via GitHub API — busca conteúdo dentro de arquivos.

        A query é construída por ``_build_code_query``: se a pergunta cita um
        repositório (ex.: ``github.com/kuzudb/kuzu``), a busca é direcionada
        com o qualifier ``repo:owner/repo`` + termos técnicos; caso contrário
        a query é normalizada (ASCII, sem acentos) para evitar o erro 422.
        """
        code_search_url = "https://api.github.com/search/code"
        q = self._build_code_query(query)
        if language:
            q = f"{q} language:{language}"

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

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um resultado bruto vindo do Github para a entidade padrão `SearchResult`.

        Args:
            raw_result (Any): O resultado bruto retornado pela API ou scraper.

        Returns:
            SearchResult: Objeto padronizado contendo os dados normalizados.
        """
        updated_at = raw_result.get("pushed_at", raw_result.get("updated_at", ""))
        license_info = raw_result.get("license") or {}

        return SearchResult(
            source="github",
            title=raw_result.get("full_name", ""),
            url=raw_result.get("html_url", ""),
            description=raw_result.get("description", "") or "",
            metrics={
                "stars": raw_result.get("stargazers_count", 0),
                "forks": raw_result.get("forks_count", 0),
                "open_issues": raw_result.get("open_issues_count", 0),
                "language": raw_result.get("language"),
                "updated_at": updated_at,
                "created_at": raw_result.get("created_at", ""),
                "license": license_info.get("spdx_id") if license_info else None,
                "topics": raw_result.get("topics", []),
                "watchers": raw_result.get("watchers_count", 0),
            },
            raw=raw_result,
        )
