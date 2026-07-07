"""
stream_monitor_agent.py — Agente de Monitoramento de Fontes em Tempo Real

Responsabilidade:
  Mantém escutas persistentes em feeds dinâmicos para atualizar o Grafo de
  Conhecimento continuamente em segundo plano, sem bloquear o pipeline principal.

  Fontes suportadas:
  - RSS/Atom Feeds: blogs técnicos, weeklies, releases de projetos OSS
  - Webhook Push (SSE remoto): endpoints que emitem eventos via text/event-stream
  - GitHub Releases: monitoramento de novas versões de bibliotecas críticas
  - arXiv New Submissions: novos papers na área temática da pesquisa (via
    export.arxiv.org, ordenados por data de submissão)

  Ciclo de vida:
  start()       → inicia workers assíncronos em background para feeds ativos
  stop()        → para os workers e aguarda conclusão
  get_feed()    → (ver get_recent_events) retorna os eventos mais recentes coletados

Integração no Pipeline:
  - Inicializado opcionalmente no Orchestrator (modo "live monitoring").
  - Os eventos são injetados como `SearchResult` extras no Grafo de Conhecimento
    através de `events_as_search_results()`.
  - Não é invocado em pesquisas pontuais — ativo apenas em sessões de
    monitoramento contínuo.

Notas desta revisão (correções aplicadas sobre a versão anterior):
  - Corrigido bug de parsing RSS: `Element.__bool__()` do ElementTree é False
    para elementos sem filhos (ex: <title>texto</title>), então o antigo
    `item.find("title") or item.find("atom:title", ns)` descartava títulos
    válidos e caía sempre no fallback Atom. Trocado por checagem `is not None`.
  - `events_as_search_results()` agora produz objetos compatíveis com o
    `SearchResult` real do SRA (src/types.py), não um dict com chaves
    inventadas (`snippet`, `published_date`, `tags`, `real_time`).
  - Implementados os parsers de arXiv (novas submissões) e Webhook/SSE que
    antes eram apenas prometidos no docstring.
  - `_seen_hashes` deixou de crescer indefinidamente: agora é podado junto
    com a expiração dos eventos (evita vazamento de memória em execuções
    longas).
  - `MonitoringFeed.is_active` agora é respeitado pelos workers (feed
    pausado não sofre polling) e passou a existir `pause_feed`/`resume_feed`/
    `remove_feed`.
  - Feeds com falhas consecutivas acima de um limite são auto-pausados
    (circuit-breaker simples) em vez de continuar martelando uma fonte
    quebrada indefinidamente.
  - GitHub Releases agora aceita token de autenticação (mesmo padrão de
    `src/search/github_searcher.py`: `config["github_token"]` →
    `Authorization: token ...`), reduzindo o limite de 60 para 5000 req/h.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

logger = logging.getLogger("stream_monitor_agent")


# ─── Constantes ───────────────────────────────────────────────────────────────

# Intervalo padrão de polling para feeds RSS/GitHub/arXiv (segundos)
DEFAULT_POLL_INTERVAL = 300  # 5 minutos
# Máximo de eventos mantidos no buffer em memória
MAX_EVENTS_BUFFER = 500
# TTL máximo de um evento antes de ser descartado (segundos)
EVENT_TTL = 3600  # 1 hora
# Intervalo da rotina de limpeza de eventos/hashes expirados (segundos)
HOUSEKEEPING_INTERVAL = 60
# Falhas consecutivas até um feed ser auto-pausado
MAX_CONSECUTIVE_ERRORS = 5
# Backoff entre tentativas de reconexão de um webhook SSE (segundos)
WEBHOOK_RECONNECT_BACKOFF = 5

SUPPORTED_SOURCE_TYPES = {"rss", "github", "arxiv", "webhook"}


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
    consecutive_errors: int = 0


@dataclass
class StreamMonitorReport:
    """Relatório de atividade do monitor em tempo real."""

    active_feeds: int = 0
    paused_feeds: int = 0
    total_events_collected: int = 0
    deduplicated_events: int = 0
    monitoring_uptime_seconds: float = 0.0
    feed_errors: dict[str, int] = field(default_factory=dict)


# ─── Parsers de Feed ──────────────────────────────────────────────────────────


def _find_text(item: ET.Element, tag: str, atom_tag: str, ns: dict[str, str]) -> str:
    """Extrai o texto de um elemento, tentando RSS 2.0 e, na ausência, Atom.

    IMPORTANTE: `Element.__bool__()` do xml.etree.ElementTree é definido pelo
    número de filhos do elemento, não por `is not None`. Um `<title>texto</title>`
    típico não tem filhos, então `if element:` é False mesmo quando o elemento
    existe e tem texto. Usar `x or y` aqui descartaria silenciosamente valores
    válidos — por isso as checagens abaixo usam sempre `is not None`.
    """
    el = item.find(tag)
    if el is None:
        el = item.find(atom_tag, ns)
    if el is None:
        return ""
    return (el.text or "").strip()


def _find_atom_text(item: ET.Element, atom_tag: str, ns: dict[str, str]) -> str:
    """Extrai o texto de uma tag Atom pura (ex: resposta do arXiv, sem RSS)."""
    el = item.find(atom_tag, ns)
    if el is None:
        return ""
    return (el.text or "").strip()


def _find_link(item: ET.Element, ns: dict[str, str]) -> str:
    el = item.find("link")
    if el is not None:
        # RSS: <link>http://...</link> (texto). Atom pode usar href.
        text = (el.text or "").strip()
        if text:
            return text
        href = el.get("href", "")
        if href:
            return href.strip()
    el = item.find("atom:link", ns)
    if el is not None:
        return (el.get("href") or (el.text or "")).strip()
    return ""


async def _parse_rss_feed(url: str, topics: list[str]) -> list[StreamEvent]:
    """Faz polling e parseia um feed RSS/Atom, retornando eventos novos."""
    events: list[StreamEvent] = []
    try:
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
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//atom:entry", ns)

        for item in items[:10]:  # Limita a 10 itens por polling
            title = _find_text(item, "title", "atom:title", ns)
            link = _find_link(item, ns)
            summary = _find_text(item, "description", "atom:summary", ns)

            if title and link:
                events.append(
                    StreamEvent(
                        source_type="rss",
                        source_url=url,
                        title=title,
                        url=link,
                        summary=summary[:500],
                        topic_tags=topics,
                    )
                )
    except ImportError:
        logger.warning(
            "[StreamMonitor] aiohttp não disponível. Polling RSS desabilitado."
        )
    except ET.ParseError as e:
        logger.warning(f"[StreamMonitor] RSS {url} retornou XML inválido: {e}")
    except Exception as e:
        logger.warning(f"[StreamMonitor] Erro ao parsear RSS {url}: {e}")
    return events


async def _parse_github_releases(
    repo: str, topics: list[str], github_token: str | None = None
) -> list[StreamEvent]:
    """Consulta a API do GitHub para listar as últimas releases de um repositório.

    Args:
        repo: Formato "owner/repo" (ex: "langchain-ai/langchain").
        topics: Tags temáticas para o evento.
        github_token: Token opcional para autenticar e elevar o rate limit
            de 60 para 5000 req/h (mesma convenção de `github_searcher.py`).
    """
    events: list[StreamEvent] = []
    try:
        import aiohttp

        api_url = f"https://api.github.com/repos/{repo}/releases?per_page=5"
        headers = {"Accept": "application/vnd.github.v3+json"}
        if github_token:
            headers["Authorization"] = f"token {github_token}"

        async with aiohttp.ClientSession() as session:
            async with session.get(
                api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 403 or resp.status == 429:
                    logger.warning(
                        f"[StreamMonitor] GitHub rate limit atingido para {repo} "
                        f"(HTTP {resp.status}). Considere configurar github_token."
                    )
                    return events
                if resp.status != 200:
                    logger.warning(
                        f"[StreamMonitor] GitHub releases {repo} retornou HTTP {resp.status}"
                    )
                    return events
                data = await resp.json()

        for release in data[:3]:
            events.append(
                StreamEvent(
                    source_type="github_release",
                    source_url=f"https://github.com/{repo}",
                    title=f"[GitHub Release] {repo}: {release.get('tag_name', 'N/A')}",
                    url=release.get("html_url", ""),
                    summary=str(release.get("body", "") or "")[:500],
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


async def _parse_arxiv_new_submissions(
    query: str, topics: list[str]
) -> list[StreamEvent]:
    """Consulta a API do arXiv por novas submissões, ordenadas por data.

    Args:
        query: Categoria (ex: "cat:cs.AI") ou termo livre (ex: "all:transformers").
            Se não vier prefixado com "cat:" ou "all:", assume-se "all:{query}".
        topics: Tags temáticas para o evento.
    """
    events: list[StreamEvent] = []
    search_query = query if ":" in query else f"all:{query}"
    try:
        import aiohttp

        api_url = "http://export.arxiv.org/api/query"
        params = {
            "search_query": search_query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": "5",
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(
                api_url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[StreamMonitor] arXiv '{query}' retornou HTTP {resp.status}"
                    )
                    return events
                content = await resp.text()

        root = ET.fromstring(content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        for entry in root.findall("atom:entry", ns):
            title = _find_atom_text(entry, "atom:title", ns)
            summary = _find_atom_text(entry, "atom:summary", ns)
            id_el = entry.find("atom:id", ns)
            link = (id_el.text or "").strip() if id_el is not None else ""

            if title and link:
                events.append(
                    StreamEvent(
                        source_type="arxiv",
                        source_url=api_url,
                        title=" ".join(title.split()),
                        url=link,
                        summary=" ".join(summary.split())[:500],
                        topic_tags=topics,
                    )
                )
    except ImportError:
        logger.warning(
            "[StreamMonitor] aiohttp não disponível. Polling arXiv desabilitado."
        )
    except ET.ParseError as e:
        logger.warning(f"[StreamMonitor] arXiv '{query}' retornou XML inválido: {e}")
    except Exception as e:
        logger.warning(f"[StreamMonitor] Erro ao consultar arXiv '{query}': {e}")
    return events


def _parse_webhook_payload(
    raw_line: str, source_url: str, topics: list[str]
) -> StreamEvent | None:
    """Converte uma linha `data: {...}` de um stream SSE em um StreamEvent.

    Payload esperado (formato livre, campos ausentes viram string vazia):
        {"title": "...", "url": "...", "summary": "..."}
    """
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError:
        return None

    title = str(payload.get("title", "")).strip()
    url = str(payload.get("url", "")).strip()
    if not title or not url:
        return None

    return StreamEvent(
        source_type="webhook",
        source_url=source_url,
        title=title,
        url=url,
        summary=str(payload.get("summary", ""))[:500],
        topic_tags=topics,
    )


# ─── Agente Principal ──────────────────────────────────────────────────────────


class StreamMonitorAgent:
    """Agente de monitoramento contínuo de fontes em tempo real para o SRA.

    Cria workers assíncronos em background que fazem polling periódico de feeds
    RSS, GitHub Releases e arXiv (e mantêm conexão persistente para Webhooks
    SSE), coletando eventos novos e adicionando-os ao buffer de eventos da
    sessão.

    Uso básico:
        agent = StreamMonitorAgent(github_token=config.github_token)
        agent.add_feed("HN RSS", "https://news.ycombinator.com/rss", "rss", ["AI"])
        agent.add_feed("LangChain", "langchain-ai/langchain", "github", ["LLM"])
        agent.add_feed("arXiv cs.AI", "cat:cs.AI", "arxiv", ["AI"])
        await agent.start()
        # ... pesquisa roda em paralelo ...
        events = agent.get_recent_events(limit=20)
        results = await agent.events_as_search_results()
        await agent.stop()
    """

    def __init__(
        self, knowledge_graph: Any = None, github_token: str | None = None
    ) -> None:
        self._kg = knowledge_graph
        self._github_token = github_token
        self._feeds: list[MonitoringFeed] = []
        self._events: list[StreamEvent] = []
        self._seen_hashes: dict[str, float] = {}  # hash -> published_at
        self._workers: list[asyncio.Task] = []
        self._housekeeping_task: asyncio.Task | None = None
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
    ) -> MonitoringFeed:
        """Registra um feed para monitoramento contínuo.

        Args:
            name: Nome legível do feed (ex: "Hacker News RSS").
            url: URL do feed RSS/webhook, "owner/repo" para GitHub, ou
                "cat:xxx"/termo livre para arXiv.
            source_type: "rss", "github", "arxiv" ou "webhook".
            topics: Tags temáticas para rotular os eventos coletados.
            poll_interval: Intervalo de polling em segundos (default: 300s).
                Ignorado para "webhook", que mantém conexão persistente.

        Returns:
            O `MonitoringFeed` registrado (útil para pausar/remover depois).

        Raises:
            ValueError: se `source_type` não for suportado.
        """
        if source_type not in SUPPORTED_SOURCE_TYPES:
            raise ValueError(
                f"source_type inválido: '{source_type}'. "
                f"Esperado um de {sorted(SUPPORTED_SOURCE_TYPES)}."
            )

        feed = MonitoringFeed(
            name=name,
            url=url,
            source_type=source_type,
            topics=topics or [],
            poll_interval=poll_interval,
        )
        self._feeds.append(feed)
        self._report.active_feeds = len([f for f in self._feeds if f.is_active])
        logger.info(f"[StreamMonitor] Feed registrado: {name} ({source_type}) → {url}")

        # Se o monitor já estiver rodando, sobe o worker imediatamente.
        if self._running:
            self._spawn_worker(feed)

        return feed

    def pause_feed(self, name: str) -> bool:
        """Pausa um feed pelo nome (o worker segue vivo, mas ocioso)."""
        for feed in self._feeds:
            if feed.name == name:
                feed.is_active = False
                logger.info(f"[StreamMonitor] Feed pausado: {name}")
                return True
        return False

    def resume_feed(self, name: str) -> bool:
        """Reativa um feed pausado (zera o contador de falhas consecutivas)."""
        for feed in self._feeds:
            if feed.name == name:
                feed.is_active = True
                feed.consecutive_errors = 0
                logger.info(f"[StreamMonitor] Feed reativado: {name}")
                return True
        return False

    def remove_feed(self, name: str) -> bool:
        """Remove um feed definitivamente do monitoramento."""
        for feed in self._feeds:
            if feed.name == name:
                feed.is_active = False
                self._feeds.remove(feed)
                logger.info(f"[StreamMonitor] Feed removido: {name}")
                return True
        return False

    def _spawn_worker(self, feed: MonitoringFeed) -> None:
        task = asyncio.create_task(
            self._poll_worker(feed),
            name=f"stream_monitor_{feed.name}",
        )
        self._workers.append(task)

    async def start(self) -> None:
        """Inicia os workers de polling em background para todos os feeds registrados."""
        if self._running:
            logger.warning("[StreamMonitor] Monitor já está em execução.")
            return

        self._running = True
        self._start_time = time.time()

        for feed in self._feeds:
            self._spawn_worker(feed)

        self._housekeeping_task = asyncio.create_task(
            self._housekeeping_loop(), name="stream_monitor_housekeeping"
        )

        logger.info(f"[StreamMonitor] {len(self._workers)} workers iniciados.")

    async def stop(self) -> None:
        """Para todos os workers de monitoramento e aguarda conclusão."""
        self._running = False

        tasks = list(self._workers)
        if self._housekeeping_task is not None:
            tasks.append(self._housekeeping_task)

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._workers.clear()
        self._housekeeping_task = None
        self._report.monitoring_uptime_seconds = time.time() - self._start_time
        logger.info(
            f"[StreamMonitor] Monitoramento encerrado. "
            f"Uptime: {self._report.monitoring_uptime_seconds:.0f}s. "
            f"Total de eventos coletados: {self._report.total_events_collected}."
        )

    async def _housekeeping_loop(self) -> None:
        """Poda periodicamente eventos e hashes expirados (evita vazamento de memória)."""
        while self._running:
            try:
                await asyncio.sleep(HOUSEKEEPING_INTERVAL)
                self._prune_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[StreamMonitor] Erro na limpeza periódica: {e}")

    def _prune_expired(self) -> None:
        now = time.time()
        before = len(self._events)
        self._events = [e for e in self._events if (now - e.published_at) <= EVENT_TTL]
        self._seen_hashes = {
            h: ts for h, ts in self._seen_hashes.items() if (now - ts) <= EVENT_TTL
        }
        pruned = before - len(self._events)
        if pruned:
            logger.info(f"[StreamMonitor] {pruned} evento(s) expirado(s) removido(s).")

    async def _poll_worker(self, feed: MonitoringFeed) -> None:
        """Worker assíncrono que faz polling periódico (ou escuta SSE) de um feed."""
        logger.info(f"[StreamMonitor] Worker iniciado para feed: {feed.name}")

        if feed.source_type == "webhook":
            await self._webhook_worker(feed)
            return

        while self._running:
            if not feed.is_active:
                await asyncio.sleep(min(feed.poll_interval, 10))
                continue
            try:
                await self._poll_feed(feed)
                feed.consecutive_errors = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._register_feed_error(feed, e)
            await asyncio.sleep(feed.poll_interval)

    def _register_feed_error(self, feed: MonitoringFeed, error: Exception) -> None:
        self._report.feed_errors[feed.name] = (
            self._report.feed_errors.get(feed.name, 0) + 1
        )
        feed.consecutive_errors += 1
        logger.warning(f"[StreamMonitor] Erro no worker de '{feed.name}': {error}")

        if feed.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            feed.is_active = False
            logger.warning(
                f"[StreamMonitor] Feed '{feed.name}' auto-pausado após "
                f"{feed.consecutive_errors} falhas consecutivas. "
                f"Use resume_feed('{feed.name}') para reativar."
            )

    async def _poll_feed(self, feed: MonitoringFeed) -> None:
        """Executa uma rodada de polling para um feed e processa novos eventos."""
        new_events: list[StreamEvent] = []

        if feed.source_type == "rss":
            new_events = await _parse_rss_feed(feed.url, feed.topics)
        elif feed.source_type == "github":
            new_events = await _parse_github_releases(
                feed.url, feed.topics, github_token=self._github_token
            )
        elif feed.source_type == "arxiv":
            new_events = await _parse_arxiv_new_submissions(feed.url, feed.topics)

        feed.last_polled_at = time.time()
        self._ingest_events(feed, new_events)

    async def _webhook_worker(self, feed: MonitoringFeed) -> None:
        """Mantém uma conexão SSE persistente, reconectando com backoff em falhas."""
        try:
            import aiohttp
        except ImportError:
            logger.warning(
                "[StreamMonitor] aiohttp não disponível. Webhook SSE desabilitado."
            )
            return

        while self._running:
            if not feed.is_active:
                await asyncio.sleep(min(feed.poll_interval, 10))
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        feed.url,
                        headers={"Accept": "text/event-stream"},
                        timeout=aiohttp.ClientTimeout(total=None, sock_read=60),
                    ) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status} de {feed.url}")

                        feed.last_polled_at = time.time()
                        feed.consecutive_errors = 0
                        async for raw_line in resp.content:
                            if not self._running or not feed.is_active:
                                break
                            line = raw_line.decode("utf-8", errors="ignore").strip()
                            if not line.startswith("data:"):
                                continue
                            event = _parse_webhook_payload(
                                line[len("data:") :].strip(), feed.url, feed.topics
                            )
                            if event is not None:
                                self._ingest_events(feed, [event])
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._register_feed_error(feed, e)
                await asyncio.sleep(WEBHOOK_RECONNECT_BACKOFF)

    def _ingest_events(
        self, feed: MonitoringFeed, new_events: list[StreamEvent]
    ) -> None:
        """Deduplica, registra e faz o buffer FIFO dos eventos recém-coletados."""
        added = 0
        for event in new_events:
            if event.event_hash not in self._seen_hashes:
                self._seen_hashes[event.event_hash] = event.published_at
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
        # Filtra eventos expirados (a poda definitiva ocorre em _prune_expired)
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
        self._report.paused_feeds = len([f for f in self._feeds if not f.is_active])
        return self._report

    async def events_as_search_results(self, limit: int = 10) -> list[Any]:
        """Converte os eventos recentes para o formato `SearchResult` do SRA.

        Permite injetar os eventos do monitor diretamente no pipeline de busca
        como se fossem resultados de um searcher convencional.

        Returns:
            Lista de `SearchResult` (src/types.py) quando o pacote do SRA está
            disponível; caso contrário, uma lista de dicts com os mesmos
            campos (uso do módulo fora do pacote, ex: testes isolados).
        """
        events = self.get_recent_events(limit=limit)

        try:
            from src.types import SearchResult  # import tardio: evita ciclo
        except ImportError:
            SearchResult = None  # type: ignore[assignment]

        results: list[Any] = []
        for e in events:
            if SearchResult is not None:
                results.append(
                    SearchResult(
                        source=f"stream_monitor:{e.source_type}",
                        title=e.title,
                        url=e.url,
                        description=e.summary,
                        metrics={
                            "published_at": e.published_at,
                            "topic_tags": e.topic_tags,
                            "real_time": True,
                        },
                        raw={"event_hash": e.event_hash, "source_url": e.source_url},
                        confidence_score=max(0.0, min(1.0, e.relevance_score)),
                        evidence_quality="unknown",
                    )
                )
            else:
                results.append(
                    {
                        "source": f"stream_monitor:{e.source_type}",
                        "title": e.title,
                        "url": e.url,
                        "description": e.summary,
                        "metrics": {
                            "published_at": e.published_at,
                            "topic_tags": e.topic_tags,
                            "real_time": True,
                        },
                        "raw": {"event_hash": e.event_hash, "source_url": e.source_url},
                        "confidence_score": max(0.0, min(1.0, e.relevance_score)),
                        "evidence_quality": "unknown",
                    }
                )
        return results

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
