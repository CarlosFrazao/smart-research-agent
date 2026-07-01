"""
RSSSearcher — ingere feeds RSS/Atom de fontes curadas de IA/tech.

Busca por relevância de keyword nos títulos e descrições dos feeds,
retornando SearchResult normalizados compatíveis com o pipeline existente.

Fontes default (espelhadas do Tino com pesos adaptados):
  Anthropic, OpenAI, DeepMind, HuggingFace, Simon Willison, Latent Space,
  arXiv cs.AI, GitHub Trending Python, Reddit LocalLLaMA, Reddit ClaudeAI,
  LangChain, Mistral, Cohere, HN Frontpage, Import AI.
"""

import re
import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from email.utils import parsedate_to_datetime

from src.search.base_searcher import BaseSearcher
from src.types import SearchResult
from src.utils.http_client import HTTPClient

logger = logging.getLogger(__name__)

# ── Feed catalog ─────────────────────────────────────────────────────────────

DEFAULT_FEEDS: List[Dict[str, Any]] = [
    {"id": "anthropic",       "name": "Anthropic News",          "url": "https://www.anthropic.com/news/rss.xml",               "weight": 1.5},
    {"id": "openai",          "name": "OpenAI News",             "url": "https://openai.com/news/rss.xml",                      "weight": 1.4},
    {"id": "deepmind",        "name": "Google DeepMind Blog",    "url": "https://deepmind.google/blog/rss.xml",                  "weight": 1.2},
    {"id": "huggingface",     "name": "Hugging Face Blog",       "url": "https://huggingface.co/blog/feed.xml",                  "weight": 1.1},
    {"id": "simonwillison",   "name": "Simon Willison",          "url": "https://simonwillison.net/atom/everything/",            "weight": 1.3},
    {"id": "latentspace",     "name": "Latent Space",            "url": "https://www.latent.space/feed",                        "weight": 1.1},
    {"id": "langchain",       "name": "LangChain Blog",          "url": "https://blog.langchain.dev/rss",                       "weight": 1.0},
    {"id": "mistral",         "name": "Mistral News",            "url": "https://mistral.ai/news/rss.xml",                      "weight": 1.0},
    {"id": "cohere",          "name": "Cohere Blog",             "url": "https://cohere.com/blog/rss.xml",                      "weight": 0.9},
    {"id": "arxiv_ai",        "name": "arXiv cs.AI",             "url": "http://export.arxiv.org/rss/cs.AI",                    "weight": 1.0},
    {"id": "hn_frontpage",    "name": "Hacker News Frontpage",   "url": "https://hnrss.org/frontpage",                          "weight": 0.8},
    {"id": "reddit_localllm", "name": "Reddit r/LocalLLaMA",     "url": "https://www.reddit.com/r/LocalLLaMA/.rss",             "weight": 0.9},
    {"id": "reddit_claudeai", "name": "Reddit r/ClaudeAI",       "url": "https://www.reddit.com/r/ClaudeAI/.rss",               "weight": 1.0},
    {"id": "import_ai",       "name": "Import AI",               "url": "https://importai.substack.com/feed",                   "weight": 1.0},
    {"id": "gh_trending_py",  "name": "GitHub Trending Python",  "url": "https://github.com/trending/python?since=weekly&format=atom", "weight": 0.8},
]

# ── Helpers ───────────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_ENTITY_MAP = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}
_ENTITY_RE = re.compile("|".join(re.escape(k) for k in _ENTITY_MAP))


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = _ENTITY_RE.sub(lambda m: _ENTITY_MAP[m.group()], text)
    return _SPACE_RE.sub(" ", text).strip()


def _parse_date(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    # Try RFC 2822 (RSS pubDate)
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    # Try ISO 8601 (Atom updated/published)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw[:19], fmt[:len(raw[:19])])
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return raw


def _stable_id(url: str, title: str) -> str:
    raw = f"{url}|{title}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _xml_text(element_text: Optional[str]) -> str:
    return _strip_html(element_text or "")


def _score_relevance(query: str, title: str, description: str, weight: float) -> float:
    """Keyword overlap score: 0-100 pontuado pela relevância da query no item."""
    if not query:
        return weight * 30.0

    terms = set(re.findall(r"\w+", query.lower()))
    terms -= {"the", "a", "an", "is", "are", "and", "or", "for", "of", "to", "in", "on", "at"}
    if not terms:
        return weight * 30.0

    haystack = f"{title} {description}".lower()
    matched = sum(1 for t in terms if t in haystack)
    overlap = matched / len(terms)
    # Base: 20 pontos de peso * overlap + bônus por título match
    title_matched = sum(1 for t in terms if t in title.lower())
    title_bonus = (title_matched / len(terms)) * 30
    return min(100.0, round((overlap * 40 + title_bonus + weight * 10), 2))


# ── Parser XML mínimo sem deps externas ──────────────────────────────────────

def _extract_tag(xml: str, tag: str) -> Optional[str]:
    m = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", xml)
    return m.group(1).strip() if m else None


def _extract_attr(xml: str, tag: str, attr: str) -> Optional[str]:
    m = re.search(rf'<{tag}[^>]*{attr}=["\']([^"\']*)["\']', xml)
    return m.group(1) if m else None


def _parse_rss_items(xml: str, feed_name: str) -> List[Dict[str, str]]:
    items = []
    for block in re.finditer(r"<item>([\s\S]*?)</item>", xml):
        raw = block.group(1)
        title = _xml_text(_extract_tag(raw, "title"))
        link = _xml_text(_extract_tag(raw, "link")) or _xml_text(_extract_tag(raw, "guid"))
        desc = _xml_text(_extract_tag(raw, "description") or _extract_tag(raw, "summary") or "")
        pub_date = _xml_text(_extract_tag(raw, "pubDate") or _extract_tag(raw, "dc:date") or "")
        if title and link:
            items.append({"title": title, "url": link, "description": desc[:600],
                          "published": _parse_date(pub_date), "feed": feed_name})
    return items


def _parse_atom_entries(xml: str, feed_name: str) -> List[Dict[str, str]]:
    items = []
    for block in re.finditer(r"<entry>([\s\S]*?)</entry>", xml):
        raw = block.group(1)
        title = _xml_text(_extract_tag(raw, "title"))
        # atom link: <link href="..."/> or <link>...</link>
        link = _extract_attr(raw, "link", "href") or _xml_text(_extract_tag(raw, "link"))
        desc = _xml_text(_extract_tag(raw, "summary") or _extract_tag(raw, "content") or "")
        pub_date = _xml_text(_extract_tag(raw, "updated") or _extract_tag(raw, "published") or "")
        if title and link:
            items.append({"title": title, "url": link, "description": desc[:600],
                          "published": _parse_date(pub_date), "feed": feed_name})
    return items


def parse_feed_xml(xml: str, feed_name: str) -> List[Dict[str, str]]:
    """Detecta RSS ou Atom e parseia os itens. Zero deps externas."""
    if not xml:
        return []
    if "<entry>" in xml:
        return _parse_atom_entries(xml, feed_name)
    return _parse_rss_items(xml, feed_name)


# ── RSSSearcher ───────────────────────────────────────────────────────────────

class RSSSearcher(BaseSearcher):
    """Busca em feeds RSS/Atom de fontes curadas de IA/tech por relevância de query."""

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config or {})
        self.http = HTTPClient(timeout=self.timeout, max_retries=2)
        feeds_cfg = (config or {}).get("feeds", DEFAULT_FEEDS)
        self.feeds: List[Dict[str, Any]] = feeds_cfg if feeds_cfg else DEFAULT_FEEDS
        self.max_feeds = (config or {}).get("max_feeds", len(self.feeds))

    async def search(self, query: str, **kwargs) -> List[SearchResult]:
        if not self.enabled:
            return []

        results: List[SearchResult] = []
        feeds_to_use = self.feeds[: self.max_feeds]

        for feed in feeds_to_use:
            try:
                feed_results = await self._fetch_feed(query, feed)
                results.extend(feed_results)
            except Exception as e:
                logger.warning(f"RSSSearcher: erro no feed '{feed['id']}': {e}")

        results.sort(key=lambda r: r.metrics.get("relevance_score", 0), reverse=True)
        return results[: self.max_results]

    async def _fetch_feed(self, query: str, feed: Dict[str, Any]) -> List[SearchResult]:
        url = feed["url"]
        feed_name = feed["name"]
        weight = float(feed.get("weight", 1.0))

        try:
            resp = await self.http.get(url, headers={"Accept": "application/rss+xml, application/atom+xml, text/xml, */*"})
            xml = resp.get("text", "")
        except Exception as e:
            logger.debug(f"RSSSearcher: falha ao buscar {url}: {e}")
            return []

        raw_items = parse_feed_xml(xml, feed_name)
        results = []
        for item in raw_items[:30]:
            result = self.normalize({**item, "weight": weight, "feed_id": feed["id"]})
            score = _score_relevance(query, result.title, result.description, weight)
            if score > 0:
                result.metrics["relevance_score"] = score
                results.append(result)

        return results

    def normalize(self, raw: Any) -> SearchResult:
        if not isinstance(raw, dict):
            return SearchResult(source="rss", title="", url="", description="")

        title = raw.get("title", "")
        url = raw.get("url", "")
        description = raw.get("description", "")
        feed_name = raw.get("feed", "rss")
        feed_id = raw.get("feed_id", "rss")
        published = raw.get("published")
        weight = float(raw.get("weight", 1.0))

        return SearchResult(
            source=f"rss:{feed_id}",
            title=title or "(sem título)",
            url=url,
            description=description[:500] if description else "",
            metrics={
                "feed_name": feed_name,
                "feed_id": feed_id,
                "published": published,
                "weight": weight,
                "relevance_score": 0.0,
                "item_id": _stable_id(url, title),
            },
            raw=raw,
        )
