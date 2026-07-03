"""Módulo de configuração centralizada do Smart Research Agent.

Lê variáveis de ambiente via pydantic-settings (arquivo .env) e expõe
uma instância tipada de `Config` com todos os parâmetros do sistema.
"""

from enum import StrEnum
import os
from dotenv import load_dotenv

# Sobrescreve as variáveis do sistema Windows pelas do .env local do workspace em produção (BUG-01)
if "PYTEST_CURRENT_TEST" not in os.environ:
    load_dotenv(override=True)

from pydantic import Field
from pydantic_settings import BaseSettings


class LLMProvider(StrEnum):
    """Provedores de LLM suportados pelo Smart Research Agent."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"
    GROQ = "groq"
    OLLAMA = "ollama"


class Config(BaseSettings):
    """Configuração global do Smart Research Agent via variáveis de ambiente.

    Lida automaticamente do arquivo `.env` na raiz do projeto.
    Todos os campos com `| None` são opcionais e habilitam funcionalidades
    específicas quando fornecidos (ex: chaves de API de scrapers, proxies, etc).
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

    max_results_per_source: int = 20
    max_iterations: int = 3
    timeout_per_source: int = 30
    output_dir: str = "./reports"
    cache_dir: str = "./.cache"
    log_level: str = "INFO"
    operation_mode: str = "cirurgia"

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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

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
