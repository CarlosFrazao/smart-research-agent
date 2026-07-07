"""Módulo de configuração centralizada do Smart Research Agent.

Lê variáveis de ambiente via pydantic-settings (arquivo `.env`) e expõe
uma instância tipada de `Config` com todos os parâmetros do sistema.

Este módulo é 100% retrocompatível com o uso anterior:

    from src.config import Config
    config = Config()

continua funcionando exatamente como antes (inclusive mutação direta de
atributos, usada hoje em `api/main.py`, `cli/main.py`, `ui/streamlit_app.py`
e `src/worker/celery_app.py`).

Além disso, o módulo passa a oferecer três capacidades novas, todas opt-in
(não alteram o comportamento de quem já usa `Config()` diretamente):

1. **Hot reload** — `config_manager` observa o arquivo `.env` via `watchdog`
   e recarrega a configuração automaticamente quando o arquivo muda, sem
   precisar reiniciar o processo (API, worker, CLI).
2. **Per-request config override** — `config_override(**overrides)` e
   `get_config()` permitem sobrescrever campos apenas durante o escopo de
   uma requisição/task, usando `contextvars` (seguro para asyncio e Celery,
   ao contrário de mutação direta em um singleton global).
3. **A/B testing de modos** — `ab_test_registry` permite registrar
   experimentos que sorteiam (de forma determinística por `subject_id`, ou
   aleatória) qual `operation_mode` usar, com contagem simples de amostras
   por variante.

Também reforça a validação: `operation_mode`, URLs, providers de captcha/proxy
e limites numéricos agora são validados pelo Pydantic — inclusive em
atribuições diretas (`config.operation_mode = "x"`), graças a
`validate_assignment=True`.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import random
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

# Sobrescreve as variáveis do sistema Windows pelas do .env local do workspace em produção (BUG-01)
if "PYTEST_CURRENT_TEST" not in os.environ:
    load_dotenv(override=True)

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class LLMProvider(StrEnum):
    """Provedores de LLM suportados pelo Smart Research Agent."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"
    GROQ = "groq"
    OLLAMA = "ollama"
    DEEPSEEK = "deepseek"
    GITHUB_MODELS = "github_models"


def _valid_operation_modes() -> tuple[str, ...] | None:
    """Lista os modos de operação válidos, sem criar import circular.

    `operation_modes.py` não importa `config.py`, então este import é seguro,
    mas é feito de forma tardia (dentro da função) para manter `config.py`
    como um módulo "folha" — importável sem depender de mais nada do `src`
    caso `operation_modes.py` venha a mudar no futuro.
    """
    try:
        from src.operation_modes import OperationModes

        return tuple(OperationModes.list_modes())
    except Exception:  # pragma: no cover - proteção contra import parcial
        logger.debug(
            "Não foi possível carregar OperationModes para validação; "
            "operation_mode não será validado contra a lista de modos.",
            exc_info=True,
        )
        return None


def _looks_like_url(value: str, schemes: tuple[str, ...]) -> bool:
    return value.startswith(schemes)


class Config(BaseSettings):
    """Configuração global do Smart Research Agent via variáveis de ambiente.

    Lida automaticamente do arquivo `.env` na raiz do projeto.
    Todos os campos com `| None` são opcionais e habilitam funcionalidades
    específicas quando fornecidos (ex: chaves de API de scrapers, proxies, etc).

    `validate_assignment=True`: atribuições diretas como
    ``config.operation_mode = "guerrilha"`` (padrão usado hoje em
    `api/main.py`, `cli/main.py`, `ui/streamlit_app.py` e
    `src/worker/celery_app.py`) passam pelas mesmas validações do construtor.
    """

    llm_provider: LLMProvider = Field(default=LLMProvider.ANTHROPIC)
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-20250514"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1"
    openrouter_api_key: str | None = None
    openrouter_model: str = "google/gemma-4-26b-a4b-it:free"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"  # free tier
    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"  # free tier
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    ollama_api_key: str | None = None

    github_models_api_key: str | None = None
    github_models_model: str = "gpt-4o-mini"
    github_models_base_url: str = "https://models.inference.ai.azure.com"

    firecrawl_api_key: str | None = None
    firecrawl_base_url: str | None = None

    spider_api_key: str = Field(default="")
    spider_base_url: str = "https://api.spider.cloud"
    spider_enabled: bool = Field(default=False)

    steel_api_key: str = Field(default="")
    steel_base_url: str = "https://api.steel.dev/v1"
    steel_enabled: bool = Field(default=False)

    # ── Anti-Blocking (Bloco 5) ───────────────────────────────────────────────
    playwright_enabled: bool = Field(default=False)
    playwright_headless: bool = Field(default=True)

    captcha_provider: str | None = Field(default=None)  # "2captcha" | "capsolver"
    captcha_api_key: str | None = Field(default=None)

    residential_proxy_provider: str | None = Field(
        default=None
    )  # "brightdata" | "smartproxy"
    residential_proxy_username: str | None = Field(default=None)
    residential_proxy_password: str | None = Field(default=None)

    # --- Firecrawl v4.30.3 Feature Flags ---
    firecrawl_redact_pii: bool = Field(default=False)
    firecrawl_lockdown_mode: bool = Field(default=False)
    firecrawl_deterministic_json: bool = Field(default=False)
    firecrawl_research_index_enabled: bool = Field(default=True)

    # --- ModelRouter: Reasoning Models ---
    reasoning_models_enabled: bool = Field(default=False)
    openai_reasoning_model: str = Field(default="o3-mini")
    deepseek_api_key: str | None = Field(default=None)
    deepseek_model: str = Field(default="deepseek-r1")
    deepseek_base_url: str = Field(default="https://api.deepseek.com/v1")

    github_token: str | None = None
    producthunt_token: str | None = None

    # ── Stream Monitor (Monitoramento em tempo real) ─────────────────────────
    enable_live_monitoring: bool = Field(
        default=False,
        description="Habilita o monitoramento contínuo de fontes em tempo real (RSS, GitHub, arXiv, Webhooks).",
    )
    monitoring_feeds: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Lista de feeds para monitorar. Formato: {'name': 'HN', 'url': '...', 'source_type': 'rss', 'topics': ['tech']}",
    )

    memory_enabled: bool = Field(default=True)
    memory_db_path: str = Field(default="reports/.research_memory.db")
    smart_routing_enabled: bool = Field(default=True)

    obsidian_vault_path: str | None = Field(default=None)
    obsidian_auto_sync: bool = Field(default=False)

    host_mode: bool = Field(default=False)
    jina_reader_base_url: str = Field(default="https://r.jina.ai/")

    semantic_scholar_api_key: str | None = Field(default=None)
    ncbi_api_key: str | None = Field(default=None)
    youtube_api_key: str | None = Field(default=None)

    max_results_per_source: int = Field(default=20, gt=0)
    max_iterations: int = Field(default=3, gt=0)
    timeout_per_source: int = Field(default=30, gt=0)
    output_dir: str = "./reports"
    cache_dir: str = "./.cache"
    log_level: str = "INFO"
    operation_mode: str = "cirurgia"

    # Budgets de pesquisa
    budget_tokens_per_query: int = Field(default=100000)
    budget_cost_per_query_usd: float = Field(default=10.0)
    budget_timeout_seconds: float = Field(default=300.0)

    # SerpAPI — fallback de último recurso para buscas na web
    serpapi_api_key: str | None = Field(default=None)
    serpapi_enabled: bool = Field(default=True)

    # Neo4j — banco de grafos para conhecimento persistente
    neo4j_uri: str | None = Field(default=None)
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: str = Field(default="password123")

    # Celery & Redis
    celery_broker_url: str = Field(default="redis://localhost:6379/0")
    celery_result_backend: str = Field(default="redis://localhost:6379/1")

    # Cohere — reranking de buscas híbridas
    cohere_api_key: str | None = Field(default=None)

    # ── Tracing distribuído (OpenTelemetry) ───────────────────────────────────
    otel_enabled: bool = Field(default=False)
    otel_service_name: str = Field(default="smart-research-agent")
    # Endpoint gRPC de um coletor OTLP (Jaeger, Tempo, OpenTelemetry Collector, etc.)
    otel_exporter_otlp_endpoint: str | None = Field(default=None)
    # Exporta spans para stdout — útil em dev local mesmo sem coletor configurado.
    otel_console_export: bool = Field(default=False)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        # Permite que `config.campo = valor` (padrão usado em api/main.py,
        # cli/main.py, ui/streamlit_app.py e worker/celery_app.py) seja
        # validado do mesmo jeito que a construção via `Config(**kwargs)`.
        "validate_assignment": True,
    }

    # ── Validadores de campo ──────────────────────────────────────────────

    @field_validator("operation_mode")
    @classmethod
    def _validate_operation_mode(cls, v: str) -> str:
        valid_modes = _valid_operation_modes()
        if valid_modes and v not in valid_modes:
            raise ValueError(
                f"operation_mode='{v}' é inválido. "
                f"Modos disponíveis: {', '.join(valid_modes)}."
            )
        return v

    @field_validator("captcha_provider")
    @classmethod
    def _validate_captcha_provider(cls, v: str | None) -> str | None:
        allowed = {"2captcha", "capsolver"}
        if v is not None and v not in allowed:
            raise ValueError(
                f"captcha_provider='{v}' inválido. Use um de: {', '.join(sorted(allowed))}."
            )
        return v

    @field_validator("residential_proxy_provider")
    @classmethod
    def _validate_proxy_provider(cls, v: str | None) -> str | None:
        allowed = {"brightdata", "smartproxy"}
        if v is not None and v not in allowed:
            raise ValueError(
                f"residential_proxy_provider='{v}' inválido. "
                f"Use um de: {', '.join(sorted(allowed))}."
            )
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            raise ValueError(
                f"log_level='{v}' inválido. Use um de: {', '.join(sorted(valid))}."
            )
        return v.upper()

    @field_validator(
        "ollama_base_url",
        "spider_base_url",
        "steel_base_url",
        "deepseek_base_url",
        "jina_reader_base_url",
        "celery_broker_url",
        "celery_result_backend",
    )
    @classmethod
    def _validate_required_url(cls, v: str | None, info) -> str | None:
        if not v or v.strip() == "" or v.strip().lower() == "none":
            return v
        schemes = (
            ("redis://",) if "celery" in info.field_name else ("http://", "https://")
        )
        if not _looks_like_url(v, schemes):
            raise ValueError(
                f"{info.field_name}='{v}' deve começar com {' ou '.join(schemes)}."
            )
        return v

    @field_validator("firecrawl_api_key")
    @classmethod
    def _validate_firecrawl_api_key(cls, v: str | None) -> str | None:
        if v in ("fc-placeholder", "fc_placeholder"):
            return None
        return v

    @field_validator("memory_db_path")
    @classmethod
    def _validate_memory_db_path(cls, v: str) -> str:
        path = Path(v)
        if not path.is_absolute():
            return str(path.resolve().absolute())
        return v

    @field_validator("firecrawl_base_url")
    @classmethod
    def _validate_optional_url(cls, v: str | None) -> str | None:
        if v is not None and not _looks_like_url(v, ("http://", "https://")):
            raise ValueError(
                f"firecrawl_base_url='{v}' deve começar com http:// ou https://."
            )
        return v

    @model_validator(mode="after")
    def _warn_if_active_provider_missing_key(self) -> "Config":
        """Avisa (sem falhar) se o provider LLM ativo não tem API key.

        Mantido como *warning*, não erro: muitos testes instanciam `Config()`
        sem nenhuma chave (usam mocks), e `Ollama` não exige chave. Falhar
        aqui quebraria esse padrão de teste já estabelecido no projeto.
        """
        key_by_provider = {
            LLMProvider.ANTHROPIC: self.anthropic_api_key,
            LLMProvider.OPENAI: self.openai_api_key,
            LLMProvider.OPENROUTER: self.openrouter_api_key,
            LLMProvider.GEMINI: self.gemini_api_key,
            LLMProvider.GROQ: self.groq_api_key,
            LLMProvider.DEEPSEEK: self.deepseek_api_key,
            LLMProvider.GITHUB_MODELS: self.github_models_api_key,
        }
        if (
            self.llm_provider in key_by_provider
            and not key_by_provider[self.llm_provider]
        ):
            logger.debug(
                "llm_provider='%s' está ativo mas sua API key não foi configurada.",
                self.llm_provider,
            )
        return self

    # ── API pública existente (inalterada) ────────────────────────────────

    def get_llm_config(self) -> dict:
        """Retorna o dicionário de configuração para o provider LLM ativo.

        Returns:
            dict: Chaves ``api_key`` e ``model`` (mais ``base_url`` para Ollama)
                  para o provider configurado em ``llm_provider``.

        Raises:
            ValueError: Se o provider configurado não for suportado.
        """
        if self.llm_provider == LLMProvider.ANTHROPIC:
            return {"api_key": self.anthropic_api_key, "model": self.anthropic_model}
        elif self.llm_provider == LLMProvider.OPENAI:
            return {"api_key": self.openai_api_key, "model": self.openai_model}
        elif self.llm_provider == LLMProvider.OPENROUTER:
            return {"api_key": self.openrouter_api_key, "model": self.openrouter_model}
        elif self.llm_provider == LLMProvider.GEMINI:
            return {"api_key": self.gemini_api_key, "model": self.gemini_model}
        elif self.llm_provider == LLMProvider.GROQ:
            return {"api_key": self.groq_api_key, "model": self.groq_model}
        elif self.llm_provider == LLMProvider.DEEPSEEK:
            return {
                "api_key": self.deepseek_api_key,
                "model": self.deepseek_model,
                "base_url": self.deepseek_base_url,
            }
        elif self.llm_provider == LLMProvider.GITHUB_MODELS:
            return {
                "api_key": self.github_models_api_key,
                "model": self.github_models_model,
                "base_url": self.github_models_base_url,
            }
        elif self.llm_provider == LLMProvider.OLLAMA:
            return {
                "base_url": self.ollama_base_url,
                "model": self.ollama_model,
                "api_key": self.ollama_api_key,
            }
        raise ValueError(f"Provider nao suportado: {self.llm_provider}")

    def get_all_llm_configs(self) -> dict:
        """Retorna configs de todos os providers para uso no failover automatico."""
        configs = {}
        if self.openrouter_api_key:
            configs["openrouter"] = {
                "api_key": self.openrouter_api_key,
                "model": self.openrouter_model,
            }
        if self.gemini_api_key:
            configs["gemini"] = {
                "api_key": self.gemini_api_key,
                "model": self.gemini_model,
            }
        if self.groq_api_key:
            configs["groq"] = {"api_key": self.groq_api_key, "model": self.groq_model}
        if self.deepseek_api_key:
            configs["deepseek"] = {
                "api_key": self.deepseek_api_key,
                "model": self.deepseek_model,
                "base_url": self.deepseek_base_url,
            }
        if self.github_models_api_key:
            configs["github_models"] = {
                "api_key": self.github_models_api_key,
                "model": self.github_models_model,
                "base_url": self.github_models_base_url,
            }
        configs["ollama"] = {
            "base_url": self.ollama_base_url,
            "model": self.ollama_model,
            "api_key": self.ollama_api_key,
        }
        return configs

    def validate_config(self) -> None:
        """Valida que a chave do Firecrawl não é o placeholder de exemplo."""
        if self.firecrawl_api_key == "fc-placeholder":
            raise ValueError(
                "A chave de API do Firecrawl está configurada como 'fc-placeholder'. "
                "Por favor, configure uma chave válida no seu arquivo .env ou no ambiente."
            )


# ═══════════════════════════════════════════════════════════════════════════
# Hot reload — observa o `.env` e recarrega a `Config` automaticamente
# ═══════════════════════════════════════════════════════════════════════════


class ConfigManager:
    """Mantém uma instância de `Config` sempre atualizada com o `.env`.

    Uso típico (ex: no `lifespan` do FastAPI em `api/main.py`, ou no boot
    do worker Celery):

        from src.config import config_manager

        config_manager.start()   # liga o watchdog
        ...
        config_manager.stop()    # no shutdown

    Enquanto o observer não é iniciado (`start()` nunca chamado), o
    `ConfigManager` funciona só como um cache simples — sem threads em
    segundo plano — o que preserva o comportamento de testes e scripts
    que nunca precisam de hot reload.
    """

    def __init__(self, env_path: str | Path = ".env", *, watch: bool = True) -> None:
        self._env_path = Path(env_path)
        self._lock = threading.RLock()
        self._config = Config()
        self._observer: Any | None = None
        self._watch_enabled = watch
        self._reload_callbacks: list[Callable[[Config], None]] = []

    @property
    def config(self) -> Config:
        """Configuração atual (sem overrides de request)."""
        with self._lock:
            return self._config

    def reload(self) -> Config:
        """Recarrega o `.env` e substitui a configuração atual atomicamente."""
        with self._lock:
            if "PYTEST_CURRENT_TEST" not in os.environ and self._env_path.exists():
                load_dotenv(str(self._env_path), override=True)
            try:
                new_config = Config()
            except Exception:
                # Um `.env` temporariamente inválido (ex: editado a meio
                # caminho por outro processo) não deve derrubar o processo
                # que está rodando com a config anterior.
                logger.exception(
                    "[ConfigManager] Falha ao recarregar configuração; mantendo a anterior."
                )
                return self._config
            self._config = new_config
            logger.info(
                "[ConfigManager] Configuração recarregada a partir de %s",
                self._env_path,
            )
            for callback in self._reload_callbacks:
                try:
                    callback(new_config)
                except Exception:
                    logger.exception("[ConfigManager] Erro em callback de reload")
            return new_config

    def on_reload(self, callback: Callable[[Config], None]) -> Callable[[Config], None]:
        """Registra callback chamado após cada reload bem-sucedido. Pode ser usado como decorator."""
        self._reload_callbacks.append(callback)
        return callback

    def start(self) -> None:
        """Liga o observer do `watchdog` sobre o diretório do `.env`."""
        if not self._watch_enabled or self._observer is not None:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning(
                "[ConfigManager] Pacote 'watchdog' não instalado; hot-reload desabilitado. "
                "Instale com `pip install watchdog` para habilitar."
            )
            return

        manager = self
        target = self._env_path.resolve()

        class _EnvChangeHandler(FileSystemEventHandler):
            def on_modified(self, event) -> None:
                if not event.is_directory and Path(event.src_path).resolve() == target:
                    manager.reload()

            on_created = on_modified
            on_moved = staticmethod(lambda event: None)

        watch_dir = str(target.parent) if target.parent != Path("") else "."
        observer = Observer()
        observer.schedule(_EnvChangeHandler(), path=watch_dir, recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer
        logger.info(
            "[ConfigManager] Hot-reload ativo, observando '%s'.", self._env_path
        )

    def stop(self) -> None:
        """Desliga o observer (chamar no shutdown da aplicação/worker)."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            logger.info("[ConfigManager] Hot-reload desligado.")


# Instância única compartilhada pelo processo. `Config()` continua podendo
# ser instanciada diretamente (não depende deste manager); este objeto é só
# a "fonte da verdade" para quem optar por `get_config()`.
config_manager = ConfigManager()


# ═══════════════════════════════════════════════════════════════════════════
# Per-request config override — seguro para asyncio (FastAPI) e Celery
# ═══════════════════════════════════════════════════════════════════════════

_override_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "sra_config_override", default=None
)


def get_config() -> Config:
    """Retorna a configuração efetiva do contexto atual.

    Combina a configuração "base" (a última recarregada pelo `config_manager`)
    com os overrides definidos por `config_override(...)` no escopo atual
    (request HTTP, task Celery, etc). Os overrides passam pela mesma
    validação de `Config`, então um `operation_mode` ou URL inválidos
    levantam `ValueError` imediatamente — em vez de silenciosamente rodar
    com um valor errado.
    """
    base = config_manager.config
    overrides = _override_ctx.get()
    if not overrides:
        return base
    merged = base.model_dump()
    merged.update(overrides)
    return Config.model_validate(merged)


@contextmanager
def config_override(**overrides: Any) -> Iterator[Config]:
    """Sobrescreve campos de `Config` apenas durante o bloco atual.

    Substitui o padrão anterior (mutar `Config()` recém-criada por
    requisição, como em `api/main.py`: ``config.operation_mode = req.mode``)
    por algo seguro para chamadas concorrentes: usa `contextvars`, que é
    isolado por request no asyncio (FastAPI) e por task no Celery — ao
    contrário de um singleton mutável compartilhado.

    Exemplo (endpoint FastAPI):

        with config_override(operation_mode=req.mode,
                              max_results_per_source=req.max_results):
            orchestrator = Orchestrator(get_config())
            result = await orchestrator.research(req.query)
    """
    current = _override_ctx.get() or {}
    token = _override_ctx.set({**current, **overrides})
    try:
        yield get_config()
    finally:
        _override_ctx.reset(token)


# ═══════════════════════════════════════════════════════════════════════════
# A/B testing de operation_mode
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ModeVariant:
    """Uma variante candidata em um experimento de A/B testing de modos."""

    operation_mode: str
    weight: float = 1.0


class ABTestExperiment:
    """Experimento de A/B testing entre `operation_mode`s.

    Faz *bucketing* determinístico por `subject_id` (ex: usuário, sessão,
    API key) usando um hash estável — o mesmo `subject_id` sempre cai na
    mesma variante enquanto o experimento existir. Sem `subject_id`, sorteia
    aleatoriamente a cada chamada, respeitando os pesos.
    """

    def __init__(self, name: str, variants: list[ModeVariant]) -> None:
        if not variants:
            raise ValueError(
                "Um experimento de A/B testing precisa de ao menos uma variante."
            )
        self.name = name
        self.variants = variants
        self._assignments: dict[str, str] = {}
        self._counts: dict[str, int] = {v.operation_mode: 0 for v in variants}
        self._lock = threading.Lock()

    def assign(self, subject_id: str | None = None) -> str:
        with self._lock:
            if subject_id:
                if subject_id not in self._assignments:
                    digest = hashlib.sha256(
                        f"{self.name}:{subject_id}".encode()
                    ).hexdigest()
                    bucket = int(digest, 16) / (16 ** len(digest))
                    self._assignments[subject_id] = self._weighted_pick(bucket)
                variant = self._assignments[subject_id]
            else:
                variant = self._weighted_pick(random.random())
            self._counts[variant] += 1
            return variant

    def _weighted_pick(self, r: float) -> str:
        total = sum(v.weight for v in self.variants)
        threshold = r * total
        cumulative = 0.0
        for variant in self.variants:
            cumulative += variant.weight
            if threshold <= cumulative:
                return variant.operation_mode
        return self.variants[-1].operation_mode

    def stats(self) -> dict[str, int]:
        """Contagem de amostras atribuídas a cada variante até agora."""
        with self._lock:
            return dict(self._counts)


class ABTestRegistry:
    """Registro central de experimentos de A/B testing de modos de operação."""

    def __init__(self) -> None:
        self._experiments: dict[str, ABTestExperiment] = {}
        self._lock = threading.Lock()

    def register(self, name: str, variants: list[ModeVariant]) -> ABTestExperiment:
        with self._lock:
            experiment = ABTestExperiment(name, variants)
            self._experiments[name] = experiment
            return experiment

    def get(self, name: str) -> ABTestExperiment | None:
        return self._experiments.get(name)

    def assign_mode(self, experiment_name: str, subject_id: str | None = None) -> str:
        experiment = self.get(experiment_name)
        if experiment is None:
            raise KeyError(
                f"Experimento de A/B testing '{experiment_name}' não registrado."
            )
        return experiment.assign(subject_id)


ab_test_registry = ABTestRegistry()


@contextmanager
def config_for_ab_test(
    experiment_name: str, subject_id: str | None = None
) -> Iterator[Config]:
    """Sorteia uma variante do experimento e aplica como `operation_mode` via `config_override`.

    Exemplo:

        ab_test_registry.register("guerrilha_vs_cirurgia", [
            ModeVariant("guerrilha", weight=0.5),
            ModeVariant("cirurgia", weight=0.5),
        ])

        with config_for_ab_test("guerrilha_vs_cirurgia", subject_id=user_id) as cfg:
            orchestrator = Orchestrator(cfg)
            result = await orchestrator.research(query)
    """
    mode = ab_test_registry.assign_mode(experiment_name, subject_id)
    with config_override(operation_mode=mode) as cfg:
        yield cfg
