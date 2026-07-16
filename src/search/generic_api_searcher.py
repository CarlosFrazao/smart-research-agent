"""GenericAPISearcher — adaptador configurável por YAML.

Transforma qualquer API REST pública em uma fonte de busca do SRA sem escrever
uma classe Python nova. A definição de cada fonte vive em
``config/generic_sources.yaml`` e é lida em runtime.

Esta é a peça central do objetivo "canivete suíço universal": adicionar uma
nova fonte passa a ser uma questão de YAML, não de código.

Design:
  - Herda de :class:`BaseSearcher` (mesmo contrato dos demais searchers).
  - Uma instância por ``source_id`` declarado no catálogo.
  - Extração de campos via JMESPath (``result_path``, ``title_field``,
    ``snippet_field``) — suporta caminhos aninhados como ``bibjson.title``.
  - Resolução de variáveis de ambiente em headers (ex: ``{CORE_API_KEY}``).
  - Zero estado HTTP persistente: usa o cliente httpx do ``BaseSearcher``.
"""

from __future__ import annotations

import logging
import os
import re
import email.utils
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

import jmespath
import yaml

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult

logger = logging.getLogger("search.generic_api")

# Cache do catálogo YAML inteiro (id -> definição). Preenchido no 1º acesso.
_SOURCE_DEF_CACHE: dict[str, dict[str, Any]] = {}

# Regex de placeholders {nome} usados em headers e url_template.
# Suporta caminhos aninhados (ex: {author.handle}) para o url_template.
_PLACEHOLDER_RE = re.compile(r"\{([\w.]+)\}")

# Caminho padrão do catálogo (raiz-do-projeto/config/generic_sources.yaml).
_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "generic_sources.yaml"
)


def _load_catalog() -> dict[str, dict[str, Any]]:
    """Carrega (com cache) o catálogo ``generic_sources.yaml`` como mapa id->def.

    Returns:
        Mapa de ``source_id`` para o dicionário de definição da fonte. Vazio se
        o arquivo não existir ou for inválido (falha graciosa e logada).
    """
    global _SOURCE_DEF_CACHE
    if _SOURCE_DEF_CACHE:
        return _SOURCE_DEF_CACHE
    try:
        with open(_CATALOG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        sources = data.get("sources", []) or []
        _SOURCE_DEF_CACHE = {
            s["id"]: s for s in sources if isinstance(s, dict) and s.get("id")
        }
    except FileNotFoundError:
        logger.warning(
            "Catálogo generic_sources.yaml não encontrado em %s", _CATALOG_PATH
        )
        _SOURCE_DEF_CACHE = {}
    except Exception as e:  # noqa: BLE001 - falha de parsing não pode quebrar o boot
        logger.warning("Falha ao carregar generic_sources.yaml: %s", e)
        _SOURCE_DEF_CACHE = {}
    return _SOURCE_DEF_CACHE


def _load_source_def(source_id: str) -> dict[str, Any] | None:
    """Retorna a definição de uma fonte do catálogo (ou ``None`` se ausente)."""
    return _load_catalog().get(source_id)


def list_generic_source_ids() -> list[str]:
    """Lista os ``id`` de todas as fontes declaradas no catálogo YAML.

    Inclui fontes com ``enabled: false`` — útil para o roteamento
    (``get_available_searchers``) conhecer todos os nomes válidos, mesmo os
    desabilitados por padrão.
    """
    return list(_load_catalog().keys())


def list_enabled_generic_source_ids() -> list[str]:
    """Lista apenas os ``id`` de fontes com ``enabled: true`` no catálogo.

    Usado pelo ``SearcherFactory`` para NÃO registrar fontes desabilitadas
    (ex: ``pypi``, ``open_meteo``) como searchers ativos.
    """
    return [sid for sid, defn in _load_catalog().items() if defn.get("enabled", True)]


def _resolve_field(item: Any, field_path: str | None) -> str:
    """Resolve um campo de ``item`` via JMESPath (ex: ``bibjson.title``).

    Args:
        item: Objeto (dict/list) de um resultado individual.
        field_path: Caminho JMESPath do campo. ``None``/vazio retorna "".

    Returns:
        Valor do campo como string (vazio se ausente ou não resolvível).
    """
    if not field_path:
        return ""
    try:
        value = jmespath.search(field_path, item)
    except Exception:  # noqa: BLE001 - expressão inválida => campo vazio
        return ""
    if value is None:
        return ""
    return str(value)


def _resolve_placeholders(template: str, source: Any) -> str:
    """Substitui ``{chave}`` em ``template`` por valores resolvidos de ``source``.

    Suporta caminhos aninhados via JMESPath (ex: ``{author.handle}``), como
    exigido por fontes como Bluesky/Mastodon (Fase 2 — Plano Parte 4).

    Args:
        template: String com placeholders ``{chave}`` (chave pode ser ponto-aninhada).
        source: dict/objeto de onde os valores são lidos.

    Returns:
        Template com placeholders resolvidos (chaves ausentes viram "").
    """
    if not template:
        return ""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        value = _resolve_field(source, key)
        return value

    return _PLACEHOLDER_RE.sub(_sub, template)


def parse_flexible_date(raw_value: Any) -> datetime | None:
    """Converte um valor de data bruto de uma API em ``datetime`` (UTC-aware).

    Aceita strings ISO-8601 (``2024-01-15T10:30:00Z``, com ou sem offset),
    timestamps numéricos (segundos ou milissegundos) e objetos ``datetime``.
    Usado pelo campo ``published_at_field`` da Fase 2 para popular
    ``SearchResult.published_at`` (que alimenta o cálculo de freshness).

    Args:
        raw_value: Valor bruto (str/int/float/datetime) ou ``None``.

    Returns:
        ``datetime`` UTC-aware, ou ``None`` se não convertível.
    """
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        # Normaliza para UTC-aware (assume UTC se naive).
        if raw_value.tzinfo is None:
            return raw_value.replace(tzinfo=dt_timezone.utc)
        return raw_value.astimezone(dt_timezone.utc)
    if isinstance(raw_value, (int, float)):
        # Timestamp: segundos se < 1e12, senão milissegundos.
        ts = float(raw_value)
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=dt_timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw_value, str):
        s = raw_value.strip()
        if not s:
            return None
        # RSS usa RFC822 (ex: "Fri, 10 Jul 2026 14:30:00 GMT").
        try:
            rfc822 = email.utils.parsedate_to_datetime(s)
            if rfc822 is not None:
                if rfc822.tzinfo is None:
                    rfc822 = rfc822.replace(tzinfo=dt_timezone.utc)
                return rfc822.astimezone(dt_timezone.utc)
        except (TypeError, ValueError):
            pass
        # ISO-8601: tenta fromisoformat (tolerante a 'Z').
        candidate = s.replace("Z", "+00:00") if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            return dt.astimezone(dt_timezone.utc)
        except ValueError:
            pass
        # Fallback para formatos comuns com espaço.
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=dt_timezone.utc)
                return dt.astimezone(dt_timezone.utc)
            except ValueError:
                continue
    return None


class GenericAPISearcher(BaseSearcher):
    """Searcher genérico configurado inteiramente por YAML.

    Uma instância por ``source_id`` declarado em ``config/generic_sources.yaml``.
    Executa a busca na API configurada e normaliza os resultados para
    :class:`SearchResult`.

    Attributes:
        source_id: Identificador da fonte (== chave no catálogo).
        source_def: Definição completa da fonte lida do YAML.
    """

    def __init__(self, source_id: str, config: dict[str, Any] | None = None):
        """Inicializa o searcher para uma fonte específica do catálogo.

        Args:
            source_id: ``id`` da fonte no ``generic_sources.yaml``.
            config: Config opcional do factory (usa apenas ``max_results``
                como fallback quando o YAML não especifica).

        Raises:
            ValueError: Se ``source_id`` não existir no catálogo.
        """
        source_def = _load_source_def(source_id)
        if not source_def:
            raise ValueError(
                f"Fonte '{source_id}' não encontrada em generic_sources.yaml"
            )

        cfg = dict(config or {})
        timeout = source_def.get("timeout", cfg.get("timeout", 15))
        max_results = source_def.get("max_results", cfg.get("max_results", 10))
        base_cfg = {
            "name": source_id,
            "timeout": timeout,
            "max_results": max_results,
            "enabled": True,
        }
        super().__init__(base_cfg)

        self.source_id = source_id
        self.source_def = source_def

    async def search(self, query: str, **kwargs: Any) -> list[SearchResult]:
        """Executa a busca na API configurada e retorna resultados normalizados.

        Args:
            query: Texto da query a buscar.
            **kwargs: ``max_results`` opcional sobrepõe o teto do YAML.

        Returns:
            Lista de :class:`SearchResult` (vazia em caso de falha — o SRA
            tolera falhas parciais por fonte).
        """
        defn = self.source_def
        max_results = int(kwargs.get("max_results") or self.max_results)

        url = str(defn["base_url"])
        params: dict[str, Any] = dict(defn.get("extra_params", {}) or {})

        query_param = defn.get("query_param")
        if query_param:
            params[query_param] = query
        else:
            url = url.replace("{query}", query)

        headers = self._build_headers(defn.get("headers", {}) or {})

        # Auth por query-param (ex: NewsAPI.org — apiKey={NEWSAPI_KEY}).
        # Se requires_api_key=true e a chave não existir, degrada graciosamente
        # (retorna []) em vez de disparar a requisição sem credencial.
        auth_type = defn.get("auth_type")
        requires_key = defn.get("requires_api_key", False)
        if auth_type == "query_api_key":
            api_key_param = defn.get("api_key_param", "apiKey")
            env_key = defn.get("env_key")
            api_key = os.environ.get(env_key or "", "") if env_key else ""
            if not api_key:
                if requires_key:
                    logger.warning(
                        "GenericAPISearcher[%s]: chave '%s' ausente — fonte pulada",
                        self.source_id,
                        env_key,
                    )
                    return []
                api_key = ""
            params[api_key_param] = api_key

        try:
            response = await self._http_request(
                "GET", url, headers=headers or None, params=params or None
            )
            data = response.json()
        except Exception as e:  # noqa: BLE001 - falha por fonte não quebra pesquisa
            logger.warning("GenericAPISearcher[%s] falhou: %s", self.source_id, e)
            return []

        raw_items = self._extract_items(data)
        results: list[SearchResult] = []
        for item in raw_items[:max_results]:
            results.append(self.normalize(item))

        logger.debug(
            "GenericAPISearcher[%s]: %d resultados para '%s'",
            self.source_id,
            len(results),
            query,
        )
        return results

    def _build_headers(self, raw_headers: dict[str, str]) -> dict[str, str]:
        """Resolve placeholders ``{ENV_VAR}`` de headers a partir de os.environ.

        Se a env var resolvida for vazia (ausente ou ``""``), o header é
        **omitido** da requisição em vez de enviado como valor vazio (ex:
        ``Bearer ``), evitando o erro ``Illegal header value`` do httpx. Um
        único ``logger.warning`` é emitido por header ignorado.

        Args:
            raw_headers: Headers com possíveis placeholders de env var.

        Returns:
            Headers com placeholders resolvidos; headers com env var
            ausente/vazia são omitidos silenciosamente (com 1 aviso).
        """
        resolved: dict[str, str] = {}
        for key, value in raw_headers.items():
            # Resolve cada placeholder {ENV_VAR} para o valor de os.environ.
            # Rastreia se algum placeholder resolveu para vazio (env var ausente
            # ou ""), pois nesse caso o header deve ser OMITIDO (e nao enviado
            # como "Bearer " vazio, que quebra o httpx com Illegal header value).
            missing_placeholder = False

            def _resolve(m: "re.Match[str]") -> str:
                nonlocal missing_placeholder
                val = os.environ.get(m.group(1), "")
                if val == "":
                    missing_placeholder = True
                return val

            rendered = _PLACEHOLDER_RE.sub(_resolve, str(value))
            if missing_placeholder:
                logger.warning(
                    "GenericAPISearcher[%s]: header '%s' ignorado — "
                    "env var ausente/vazia no placeholder.",
                    self.source_id,
                    key,
                )
                continue
            resolved[key] = rendered
        return resolved

    def _extract_items(self, data: Any) -> list[Any]:
        """Extrai a lista de resultados brutos da resposta via ``result_path``.

        Args:
            data: Payload JSON parseado da API.

        Returns:
            Lista de itens brutos (vazia se o caminho não resolver uma lista).
        """
        result_path = self.source_def.get("result_path")
        if not result_path:
            # Resposta é diretamente uma lista no nível raiz.
            return data if isinstance(data, list) else []
        try:
            extracted = jmespath.search(result_path, data)
        except Exception:  # noqa: BLE001 - path inválido => sem resultados
            return []
        if isinstance(extracted, list):
            return extracted
        return []

    def normalize(self, raw_result: Any) -> SearchResult:
        """Normaliza um item bruto da API para :class:`SearchResult`.

        Usa os mapeamentos ``title_field``, ``snippet_field`` e ``url_template``
        da definição YAML da fonte.

        Args:
            raw_result: Item individual retornado pela API.

        Returns:
            :class:`SearchResult` normalizado (campos ausentes viram "").
        """
        defn = self.source_def
        title = _resolve_field(raw_result, defn.get("title_field", "title"))
        snippet = _resolve_field(raw_result, defn.get("snippet_field"))
        item_url = _resolve_placeholders(defn.get("url_template", ""), raw_result)

        result = SearchResult(
            source=self.source_id,
            title=title,
            url=item_url,
            description=snippet,
        )

        # published_at (Fase 1/2): alimenta o cálculo de freshness. Campo
        # especial — quando presente no YAML, é parseado para datetime UTC.
        published_at_field = defn.get("published_at_field")
        if published_at_field:
            raw_date = _resolve_field(raw_result, published_at_field)
            result.published_at = parse_flexible_date(raw_date)

        # metrics_fields (opcional): mapa nome->JMESPath gravado em metrics.
        metrics_fields = defn.get("metrics_fields")
        if isinstance(metrics_fields, dict):
            for metric_name, metric_path in metrics_fields.items():
                value = _resolve_field(raw_result, metric_path)
                if value not in ("", None):
                    result.metrics[metric_name] = value

        return result

    async def close(self) -> None:
        """Fecha o cliente HTTP herdado do BaseSearcher, se aberto."""
        await super().close()
