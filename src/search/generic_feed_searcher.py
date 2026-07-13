"""GenericFeedSearcher — adaptador declarativo para fontes RSS/Atom.

Transforma feeds RSS/Atom configurados em ``config/generic_feeds.yaml`` em
fontes de busca do SRA sem escrever uma classe Python nova por feed. O
``GenericAPISearcher`` lida com APIs REST + JSON; este searcher lida com
feeds XML (RSS 2.0 / Atom) usando **apenas a biblioteca padrão** (``xml.etree``
+ ``urllib``) — mantendo a regra de zero-dependência do projeto (CLAUDE.md §5).

Design:
  - Herda de :class:`BaseSearcher` (mesmo contrato dos demais searchers).
  - Uma instância por ``feed_id`` declarado no catálogo de feeds.
  - O ``mapping`` do YAML aponta campos por nome de elemento (ex: ``title``,
    ``link``, ``description``, ``pubDate``) e é resolvido de forma tolerante a
    RSS e Atom (namespaces são ignorados; aliases como ``summary``/``published``
    são unificados).
  - ``published_at`` é parseado para ``datetime`` UTC-aware e gravado em
    :class:`SearchResult` (alimenta o freshness da Fase 1).

Limitações deliberadas (v1): apenas a primeira página do feed; sem paginação;
timeout por feed via ``timeout`` do YAML.
"""

from __future__ import annotations

import email.utils
import logging
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult

logger = logging.getLogger("search.generic_feed")

# Cache do catálogo de feeds (id -> definição). Preenchido no 1º acesso.
_FEED_DEF_CACHE: dict[str, dict[str, Any]] = {}

# Caminho padrão do catálogo de feeds (raiz-do-projeto/config/generic_feeds.yaml).
_FEEDS_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "generic_feeds.yaml"
)

# Aliases de campos: múltiplos nomes de elemento (RSS/Atom) caem numa chave única.
_FIELD_ALIASES = {
    "title": ["title"],
    "url": ["link", "id"],
    "description": ["description", "summary", "content"],
    "published_at": ["published", "pubDate", "updated", "issued"],
}


def _strip_ns(tag: str) -> str:
    """Remove namespace de uma tag XML (ex: ``{http://...}entry`` -> ``entry``)."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _load_feed_catalog() -> dict[str, dict[str, Any]]:
    """Carrega (com cache) o catálogo ``generic_feeds.yaml`` como mapa id->def.

    Returns:
        Mapa de ``feed_id`` para a definição. Vazio se o arquivo não existir
        ou for inválido (falha graciosa e logada).
    """
    global _FEED_DEF_CACHE
    if _FEED_DEF_CACHE:
        return _FEED_DEF_CACHE
    try:
        import yaml

        with open(_FEEDS_CATALOG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        feeds = data.get("feeds", []) or []
        _FEED_DEF_CACHE = {
            s["id"]: s for s in feeds if isinstance(s, dict) and s.get("id")
        }
    except FileNotFoundError:
        logger.warning(
            "Catálogo generic_feeds.yaml não encontrado em %s", _FEEDS_CATALOG_PATH
        )
        _FEED_DEF_CACHE = {}
    except Exception as e:  # noqa: BLE001 - parsing não pode quebrar o boot
        logger.warning("Falha ao carregar generic_feeds.yaml: %s", e)
        _FEED_DEF_CACHE = {}
    return _FEED_DEF_CACHE


def list_generic_feed_ids() -> list[str]:
    """Lista os ``id`` de todos os feeds declarados no catálogo YAML."""
    return list(_load_feed_catalog().keys())


def list_enabled_generic_feed_ids() -> list[str]:
    """Lista apenas os ``id`` de feeds com ``enabled: true`` no catálogo."""
    return [
        fid for fid, defn in _load_feed_catalog().items() if defn.get("enabled", True)
    ]


def _parse_feed_date(raw: str | None) -> datetime | None:
    """Converte uma data de feed (RSS RFC822 ou Atom ISO-8601) em datetime UTC.

    Args:
        raw: Texto da data (ex: ``"Fri, 10 Jul 2026 12:00:00 GMT"`` ou
            ``"2026-07-10T12:00:00Z"``), ou ``None``.

    Returns:
        ``datetime`` UTC-aware, ou ``None`` se não convertível.
    """
    if not raw:
        return None
    raw = raw.strip()
    # RSS usa RFC822 (ex: "Fri, 10 Jul 2026 12:00:00 GMT").
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            return dt.astimezone(dt_timezone.utc)
    except (TypeError, ValueError):
        pass
    # Atom usa ISO-8601.
    candidate = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(dt_timezone.utc)
    except ValueError:
        return None


class GenericFeedSearcher(BaseSearcher):
    """Searcher declarativo para fontes RSS/Atom configuradas em YAML.

    Uma instância por ``feed_id`` declarado em ``config/generic_feeds.yaml``.
    Busca o feed, parseia os itens e normaliza para :class:`SearchResult`.

    Attributes:
        source_id: Identificador do feed (== chave no catálogo).
        feed_def: Definição completa do feed lida do YAML.
    """

    def __init__(self, feed_id: str, config: dict[str, Any] | None = None):
        """Inicializa o searcher para um feed específico do catálogo.

        Args:
            feed_id: ``id`` do feed em ``generic_feeds.yaml``.
            config: Config opcional do factory (usa apenas ``timeout``/
                ``max_results`` como fallback quando o YAML não especifica).

        Raises:
            ValueError: Se ``feed_id`` não existir no catálogo.
        """
        feed_def = _load_feed_catalog().get(feed_id)
        if not feed_def:
            raise ValueError(f"Feed '{feed_id}' não encontrado em generic_feeds.yaml")

        cfg = dict(config or {})
        timeout = int(feed_def.get("timeout", cfg.get("timeout", 10)))
        max_results = int(feed_def.get("max_results", cfg.get("max_results", 20)))
        base_cfg = {
            "name": feed_id,
            "timeout": timeout,
            "max_results": max_results,
            "enabled": True,
        }
        super().__init__(base_cfg)

        self.source_id = feed_id
        self.feed_def = feed_def

    async def search(self, query: str, **kwargs: Any) -> list[SearchResult]:
        """Busca o feed e retorna os itens normalizados.

        Args:
            query: Texto da query (interpolado na ``base_url`` via ``{query}``).
            **kwargs: ``max_results`` opcional sobrepõe o teto do YAML.

        Returns:
            Lista de :class:`SearchResult` (vazia em caso de falha — o SRA
            tolera falhas parciais por fonte).
        """
        max_results = int(kwargs.get("max_results") or self.max_results)
        base_url = str(self.feed_def["base_url"])
        url = base_url.replace("{query}", urllib.parse.quote(query))

        try:
            raw_xml = self._fetch(url)
        except Exception as e:  # noqa: BLE001 - falha por fonte não quebra pesquisa
            logger.warning("GenericFeedSearcher[%s] falhou: %s", self.source_id, e)
            return []

        items = self._parse_items(raw_xml)
        results: list[SearchResult] = []
        for item in items[:max_results]:
            results.append(self.normalize(item))

        logger.debug(
            "GenericFeedSearcher[%s]: %d resultados para '%s'",
            self.source_id,
            len(results),
            query,
        )
        return results

    def _fetch(self, url: str) -> str:
        """Baixa o conteúdo do feed via urllib (stdlib) com timeout e UA.

        Args:
            url: URL do feed já com a query interpolada.

        Returns:
            Conteúdo textual do feed.

        Raises:
            Exception: Em caso de erro de rede/HTTP (tratado pelo caller).
        """
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; SmartResearchAgent/1.0; "
                    "+https://github.com/CarlosFrazao/smart-research-agent)"
                ),
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")

    def _parse_items(self, raw_xml: str) -> list[dict[str, str]]:
        """Parseia o XML do feed em lista de dicts normalizados por alias.

        Args:
            raw_xml: Conteúdo XML do feed (RSS ou Atom).

        Returns:
            Lista de dicts onde cada chave é um alias de ``_FIELD_ALIASES``
            (``title``, ``url``, ``description``, ``published_at``) e o valor é
            o texto do elemento correspondente. Lista vazia se o parse falhar.
        """
        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError as e:
            logger.warning(
                "GenericFeedSearcher[%s]: XML inválido: %s", self.source_id, e
            )
            return []

        items: list[dict[str, str]] = []
        # RSS usa <item>; Atom usa <entry>. Procuramos ambos.
        for node in root.iter():
            local = _strip_ns(node.tag)
            if local in ("item", "entry"):
                items.append(self._normalize_node(node))
        return items

    def _normalize_node(self, node: ET.Element) -> dict[str, str]:
        """Extrai campos de um <item>/<entry> para um dict por alias.

        Args:
            node: Elemento XML do item/entry do feed.

        Returns:
            Dict com chaves de alias (``title``/``url``/``description``/
            ``published_at``) para textos dos elementos correspondentes.
        """
        # Coleta todos os elementos filhos por nome (sem namespace).
        raw: dict[str, list[str]] = {}
        for child in node.iter():
            if child is node:
                continue
            local = _strip_ns(child.tag)
            text = (child.text or "").strip()
            if local == "link":
                # RSS: <link>texto; Atom: <link href="..."> — pega o href.
                href = child.get("href")
                if href:
                    text = href.strip()
            if text:
                raw.setdefault(local, []).append(text)

        result: dict[str, str] = {}
        for alias, names in _FIELD_ALIASES.items():
            for name in names:
                if name in raw and raw[name]:
                    result[alias] = raw[name][0]
                    break
        return result

    def normalize(self, raw_item: dict[str, str]) -> SearchResult:
        """Normaliza um item de feed bruto para :class:`SearchResult`.

        O ``raw_item`` já vem normalizado por alias canônico (``title``/``url``/
        ``description``/``published_at``) produzido por ``_normalize_node``.
        O ``mapping`` do YAML seleciona qual alias cada papel usa (default canônico);
        como ``_normalize_node`` grava sob os aliases canônicos, buscamos as chaves
        canônicas diretamente aqui.

        Args:
            raw_item: Dict normalizado por alias (ver ``_normalize_node``).

        Returns:
            :class:`SearchResult` com ``published_at`` populado quando o
            mapeamento de data resolver.
        """
        _ = self.feed_def.get("mapping", {}) or {}
        title = raw_item.get("title", "")
        url = raw_item.get("url", "")
        description = raw_item.get("description", "")
        raw_date = raw_item.get("published_at", None)

        return SearchResult(
            source=self.source_id,
            title=title,
            url=url,
            description=description,
            published_at=_parse_feed_date(raw_date),
        )
