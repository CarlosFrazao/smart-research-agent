"""
stream_monitor_agent.py — Agente de Monitoramento de Fontes em Tempo Real

Responsabilidade:
  Mantém escutas persistentes em feeds dinâmicos para atualizar o Grafo de
  Conhecimento continuamente em segundo plano, sem bloquear o pipeline principal.

  Fontes suportadas:
  - RSS/Atom Feeds: blogs técnicos, weeklies, releases de projetos OSS
  - Webhook Push (SSE remoto): endpoints de APIs que emitem eventos em tempo real
  - GitHub Releases: monitoramento de novas versões de bibliotecas críticas
  - arXiv New Submissions: novos papers na área temática da pesquisa

  Ciclo de vida:
  start(topics) → inicia workers assíncronos em background
  stop()        → para os workers e aguarda conclusão
  get_feed()    → retorna os eventos mais recentes coletados (última hora)

Integração no Pipeline:
  - Inicializado opcionalmente no Orchestrator (modo "live monitoring").
  - Os eventos são injetados como SearchResult extras no Grafo de Conhecimento.
  - Não é invocado em pesquisas pontuais — ativo apenas em sessões de monitoramento contínuo.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

logger = logging.getLogger("stream_monitor_agent")


# ─── Constantes ───────────────────────────────────────────────────────────────

# Intervalo padrão de polling para feeds RSS (segundos)
DEFAULT_POLL_INTERVAL = 300  # 5 minutos
# Máximo de eventos mantidos no buffer em memória
MAX_EVENTS_BUFFER = 500
# TTL máximo de um evento antes de ser descartado (segundos)
EVENT_TTL = 3600  # 1 hora


# ─── Data Contracts ────────────────────────────────────────────────────────────


@dataclass
class StreamEvent:
    """Evento capturado de uma fonte de monitoramento em tempo real."""

    source_type: str  # "rss", "github_release", "arxiv", "webhook"
    source_url: str
    title: str
    url: str
    summary: str = ""
    published_at: float = field(default_factory=time.time)
    event_hash: str = ""  # Hash para deduplicação
    topic_tags: list[str] = field(default_factory=list)
    relevance_score: float = 0.0  # 0.0-1.0 (calculado por similaridade semântica)

    def __post_init__(self) -> None:
        if not self.event_hash:
            self.event_hash = hashlib.md5(
                f"{self.url}{self.title}".encode()
            ).hexdigest()

    @property
    def age_seconds(self) -> float:
        """Tempo em segundos desde a publicação do evento."""
        return time.time() - self.published_at

    @property
    def is_expired(self) -> bool:
        """Indica se o evento passou do TTL configurado."""
        return self.age_seconds > EVENT_TTL


@dataclass
class MonitoringFeed:
    """Feed de monitoramento configurado."""

    name: str
    url: str
    source_type: str  # "rss", "github", "arxiv", "webhook"
    topics: list[str] = field(default_factory=list)
    poll_interval: int = DEFAULT_POLL_INTERVAL
    is_active: bool = True
    last_polled_at: float = 0.0
    events_collected: int = 0


@dataclass
class StreamMonitorReport:
    """Relatório de atividade do monitor em tempo real."""

    active_feeds: int = 0
    total_events_collected: int = 0
    deduplicated_events: int = 0
    monitoring_uptime_seconds: float = 0.0
    feed_errors: dict[str, int] = field(default_factory=dict)


# ─── Parsers de Feed ──────────────────────────────────────────────────────────


async def _parse_rss_feed(url: str, topics: list[str]) -> list[StreamEvent]:
    """Faz polling e parseia um feed RSS/Atom, retornando eventos novos."""
    events: list[StreamEvent] = []
    try:
        import xml.etree.ElementTree as ET
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[StreamMonitor] RSS {url} retornou HTTP {resp.status}"
                    )
                    return events
                content = await resp.text()

        root = ET.fromstring(content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        # Suporte a RSS 2.0 e Atom
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)

        for item in items[:10]:  # Limita a 10 itens por polling
            title_el = item.find("title") or item.find("atom:title", ns)
            link_el = item.find("link") or item.find("atom:link", ns)
            summary_el = item.find("description") or item.find("atom:summary", ns)

            title = title_el.text if title_el is not None and title_el.text else ""
            link = (
                (link_el.text or link_el.get("href", "")) if link_el is not None else ""
            )
            summary = (
                summary_el.text if summary_el is not None and summary_el.text else ""
            )

            if title and link:
                events.append(
                    StreamEvent(
                        source_type="rss",
                        source_url=url,
                        title=title.strip(),
                        url=link.strip(),
                        summary=summary[:500].strip(),
                        topic_tags=topics,
                    )
                )
    except ImportError:
        logger.warning(
            "[StreamMonitor] aiohttp não disponível. Polling RSS desabilitado."
        )
    except Exception as e:
        logger.warning(f"[StreamMonitor] Erro ao parsear RSS {url}: {e}")
    return events


async def _parse_github_releases(repo: str, topics: list[str]) -> list[StreamEvent]:
    """Consulta a API do GitHub para listar as últimas releases de um repositório.

    Args:
        repo: Formato "owner/repo" (ex: "langchain-ai/langchain").
        topics: Tags temáticas para o evento.
    """
    events: list[StreamEvent] = []
    try:
        import aiohttp

        api_url = f"https://api.github.com/repos/{repo}/releases?per_page=5"

        async with aiohttp.ClientSession() as session:
            headers = {"Accept": "application/vnd.github.v3+json"}
            async with session.get(
                api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return events
                data = await resp.json()

        for release in data[:3]:
            events.append(
                StreamEvent(
                    source_type="github_release",
                    source_url=f"https://github.com/{repo}",
                    title=f"[GitHub Release] {repo}: {release.get('tag_name', 'N/A')}",
                    url=release.get("html_url", ""),
                    summary=str(release.get("body", ""))[:500],
                    topic_tags=topics,
                )
            )
    except ImportError:
        logger.warning(
            "[StreamMonitor] aiohttp não disponível. GitHub polling desabilitado."
        )
    except Exception as e:
        logger.warning(
            f"[StreamMonitor] Erro ao consultar GitHub releases para {repo}: {e}"
        )
    return events


# ─── Agente Principal ──────────────────────────────────────────────────────────


class StreamMonitorAgent:
    """Agente de monitoramento contínuo de fontes em tempo real para o SRA.

    Cria workers assíncronos em background que fazem polling periódico de feeds
    RSS, GitHub Releases e arXiv, coletando eventos novos e adicionando-os ao
    buffer de eventos da sessão.

    Uso básico:
        agent = StreamMonitorAgent()
        agent.add_feed("HN RSS", "https://news.ycombinator.com/rss", "rss", ["AI"])
        agent.add_feed("LangChain", "langchain-ai/langchain", "github", ["LLM"])
        await agent.start()
        # ... pesquisa roda em paralelo ...
        events = agent.get_recent_events(limit=20)
        await agent.stop()
    """

    def __init__(self, knowledge_graph: Any = None) -> None:
        self._kg = knowledge_graph
        self._feeds: list[MonitoringFeed] = []
        self._events: list[StreamEvent] = []
        self._seen_hashes: set[str] = set()
        self._workers: list[asyncio.Task] = []
        self._running = False
        self._start_time = 0.0
        self._report = StreamMonitorReport()
        logger.info("StreamMonitorAgent inicializado.")

    def add_feed(
        self,
        name: str,
        url: str,
        source_type: str,
        topics: list[str] | None = None,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        """Registra um feed para monitoramento contínuo.

        Args:
            name: Nome legível do feed (ex: "Hacker News RSS").
            url: URL do feed RSS ou "owner/repo" para GitHub.
            source_type: "rss", "github", ou "arxiv".
            topics: Tags temáticas para rotular os eventos coletados.
            poll_interval: Intervalo de polling em segundos (default: 300s).
        """
        feed = MonitoringFeed(
            name=name,
            url=url,
            source_type=source_type,
            topics=topics or [],
            poll_interval=poll_interval,
        )
        self._feeds.append(feed)
        self._report.active_feeds = len(self._feeds)
        logger.info(f"[StreamMonitor] Feed registrado: {name} ({source_type}) → {url}")

    async def start(self) -> None:
        """Inicia os workers de polling em background para todos os feeds registrados."""
        if self._running:
            logger.warning("[StreamMonitor] Monitor já está em execução.")
            return

        self._running = True
        self._start_time = time.time()

        for feed in self._feeds:
            task = asyncio.create_task(
                self._poll_worker(feed),
                name=f"stream_monitor_{feed.name}",
            )
            self._workers.append(task)

        logger.info(f"[StreamMonitor] {len(self._workers)} workers iniciados.")

    async def stop(self) -> None:
        """Para todos os workers de monitoramento e aguarda conclusão."""
        self._running = False
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._report.monitoring_uptime_seconds = time.time() - self._start_time
        logger.info(
            f"[StreamMonitor] Monitoramento encerrado. "
            f"Uptime: {self._report.monitoring_uptime_seconds:.0f}s. "
            f"Total de eventos coletados: {self._report.total_events_collected}."
        )

    async def _poll_worker(self, feed: MonitoringFeed) -> None:
        """Worker assíncrono que faz polling periódico de um feed específico."""
        logger.info(f"[StreamMonitor] Worker iniciado para feed: {feed.name}")
        while self._running:
            try:
                await self._poll_feed(feed)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._report.feed_errors[feed.name] = (
                    self._report.feed_errors.get(feed.name, 0) + 1
                )
                logger.warning(f"[StreamMonitor] Erro no worker de '{feed.name}': {e}")
            await asyncio.sleep(feed.poll_interval)

    async def _poll_feed(self, feed: MonitoringFeed) -> None:
        """Executa uma rodada de polling para um feed e processa novos eventos."""
        new_events: list[StreamEvent] = []

        if feed.source_type == "rss":
            new_events = await _parse_rss_feed(feed.url, feed.topics)
        elif feed.source_type == "github":
            new_events = await _parse_github_releases(feed.url, feed.topics)
        # TODO: adicionar parsers para arxiv e webhook push

        feed.last_polled_at = time.time()
        added = 0
        for event in new_events:
            if event.event_hash not in self._seen_hashes:
                self._seen_hashes.add(event.event_hash)
                self._events.append(event)
                feed.events_collected += 1
                self._report.total_events_collected += 1
                added += 1
            else:
                self._report.deduplicated_events += 1

        # Manter buffer dentro do limite máximo (FIFO)
        if len(self._events) > MAX_EVENTS_BUFFER:
            self._events = self._events[-MAX_EVENTS_BUFFER:]

        if added > 0:
            logger.info(
                f"[StreamMonitor] {feed.name}: {added} novos eventos coletados."
            )

    def get_recent_events(
        self,
        limit: int = 20,
        topic_filter: str | None = None,
    ) -> list[StreamEvent]:
        """Retorna os eventos mais recentes coletados, com filtragem opcional por tópico.

        Args:
            limit: Número máximo de eventos a retornar.
            topic_filter: Filtra por eventos que contenham esta tag de tópico.

        Returns:
            Lista de StreamEvent ordenada do mais recente para o mais antigo.
        """
        # Filtra eventos expirados
        active = [e for e in self._events if not e.is_expired]

        if topic_filter:
            active = [
                e
                for e in active
                if any(topic_filter.lower() in tag.lower() for tag in e.topic_tags)
            ]

        # Ordena do mais recente para o mais antigo
        active.sort(key=lambda e: e.published_at, reverse=True)
        return active[:limit]

    def get_report(self) -> StreamMonitorReport:
        """Retorna o relatório de atividade do monitor."""
        self._report.active_feeds = len([f for f in self._feeds if f.is_active])
        return self._report

    async def events_as_search_results(self, limit: int = 10) -> list[dict[str, Any]]:
        """Converte os eventos recentes para o formato SearchResult do SRA.

        Permite injetar os eventos do monitor diretamente no pipeline de busca
        como se fossem resultados de um searcher convencional.

        Returns:
            Lista de dicionários compatíveis com SearchResult do SRA.
        """
        events = self.get_recent_events(limit=limit)
        return [
            {
                "title": e.title,
                "url": e.url,
                "snippet": e.summary,
                "source": e.source_type,
                "published_date": e.published_at,
                "relevance_score": e.relevance_score,
                "tags": e.topic_tags,
                "real_time": True,
            }
            for e in events
        ]

    async def stream_events(self) -> AsyncGenerator[StreamEvent, None]:
        """Gerador assíncrono que emite eventos conforme são coletados (streaming).

        Útil para integração com endpoints SSE ou websockets para atualização
        em tempo real da UI Streamlit ou de dashboards externos.
        """
        last_seen_count = len(self._events)
        while self._running:
            current_count = len(self._events)
            if current_count > last_seen_count:
                for event in self._events[last_seen_count:current_count]:
                    yield event
                last_seen_count = current_count
            await asyncio.sleep(1.0)
