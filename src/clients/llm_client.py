"""
LLMClient — cliente unificado com failover automático entre providers.

Ordem de failover ao receber 429 / 503 / RateLimitError:
  1. OpenRouter  (free tier)
  2. Gemini      (gemini-2.5-flash — free tier)
  3. Groq        (llama-3.3-70b-versatile — free tier)
  4. Ollama      (local — fallback final)

O provider ativo é determinado por LLM_PROVIDER no .env.
Se ele falhar com rate-limit, a cadeia acima é tentada automaticamente.

Retry com backoff exponencial:
  Antes de acionar o failover, tenta até RATE_LIMIT_MAX_RETRIES vezes
  com espera progressiva (1s, 2s, 4s) para absorver picos transitórios de 429.
"""

import asyncio
import json
import logging
import re
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from src.utils.retry import RetryConfig
from src.monitoring.tracing import trace_llm_call

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class LLMClientError(RuntimeError):
    """Erro genérico do cliente LLM (após esgotar retries)."""

    pass


class OutputValidationError(LLMClientError):
    """A resposta do modelo não pôde ser validada contra o Pydantic schema."""

    pass


class StructuredGenerationError(RuntimeError):
    """Falha de geração estruturada (LLM retornou vazio/inválido).

    Não é estourada para o caller — `generate_structured` retorna um fallback
    seguro (`[]`/`{}`). O sinal visível fica em `LLMClient.last_failure`.
    """

    pass


def _retryable_exceptions_for(
    provider: "LLMProvider",
) -> tuple[type[BaseException], ...]:
    if provider in (LLMProvider.OPENAI, LLMProvider.OPENROUTER, LLMProvider.OLLAMA):
        try:
            from openai import (
                APIConnectionError,
                APITimeoutError,
                RateLimitError,
                InternalServerError,
            )

            return (
                APITimeoutError,
                APIConnectionError,
                RateLimitError,
                InternalServerError,
            )
        except ImportError:
            return (TimeoutError, ConnectionError)
    elif provider == LLMProvider.ANTHROPIC:
        try:
            from anthropic import (
                APIConnectionError,
                APITimeoutError,
                RateLimitError,
                InternalServerError,
            )

            return (
                APITimeoutError,
                APIConnectionError,
                RateLimitError,
                InternalServerError,
            )
        except ImportError:
            return (TimeoutError, ConnectionError)
    elif provider == LLMProvider.GROQ:
        try:
            from groq import (
                APIConnectionError,
                APITimeoutError,
                RateLimitError,
                InternalServerError,
            )

            return (
                APITimeoutError,
                APIConnectionError,
                RateLimitError,
                InternalServerError,
            )
        except ImportError:
            return (TimeoutError, ConnectionError)
    else:
        return (TimeoutError, ConnectionError)


# ── Retry com backoff exponencial antes de acionar failover ───────────────────
RATE_LIMIT_MAX_RETRIES: int = 3
RATE_LIMIT_INITIAL_WAIT_S: float = 1.0  # 1s, 2s, 4s

# ── Códigos / mensagens que indicam rate-limit ou indisponibilidade ──────────
_RATE_LIMIT_CODES = {429, 503, 529}
_RATE_LIMIT_MESSAGES = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "temporarily rate-limited",
    "overloaded",
    "model is currently overloaded",
    "upstream",
)


def _is_rate_limit(exc: Exception) -> bool:
    """Retorna True se a exceção é causada por rate-limit ou sobrecarga."""
    msg = str(exc).lower()
    if any(kw in msg for kw in _RATE_LIMIT_MESSAGES):
        return True
    # openai.RateLimitError / openai.APIStatusError têm .status_code
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return code in _RATE_LIMIT_CODES


_DAILY_QUOTA_MESSAGES = (
    "free-models-per-day",
    "daily limit",
    "quota exceeded",
    "free model requests",
)


def _is_daily_quota(exc: Exception) -> bool:
    """Retorna True se é cota diária esgotada (sem retry, ir direto ao failover)."""
    msg = str(exc).lower()
    return any(kw in msg for kw in _DAILY_QUOTA_MESSAGES)


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"
    GROQ = "groq"
    OLLAMA = "ollama"
    DEEPSEEK = "deepseek"
    GITHUB_MODELS = "github_models"


class LLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        config: dict[str, Any],
        model_router=None,
        # configs de fallback (extraídas do Config global pelo Orchestrator)
        fallback_configs: dict[str, dict[str, Any]] | None = None,
    ):
        self.provider = provider
        self.config = config.copy() if config else {}
        self._client = None
        self.model = ""
        self.model_router = model_router
        # fallback_configs: {"gemini": {...}, "groq": {...}, "openrouter": {...}, "ollama": {...}}
        self._fallback_configs: dict[str, dict[str, Any]] = fallback_configs or {}

        # Inicializa a lista de chaves de API com suporte a rotação dinâmica
        api_key_raw = self.config.get("api_key") or ""
        if isinstance(api_key_raw, str) and "," in api_key_raw:
            self._api_keys = [k.strip() for k in api_key_raw.split(",") if k.strip()]
        else:
            self._api_keys = [api_key_raw] if api_key_raw else []
        self._current_key_idx = 0
        if self._api_keys:
            self.config["api_key"] = self._api_keys[0]

        # Inicializa a lista de modelos com suporte a rotação dinâmica
        model_raw = self.config.get("model") or ""
        if isinstance(model_raw, str) and "," in model_raw:
            self._models = [m.strip() for m in model_raw.split(",") if m.strip()]
        else:
            self._models = [model_raw] if model_raw else []
        self._current_model_idx = 0

        self._init_providers_safely()
        self._init_client()
        from src.token_economy import TokenEconomy

        self.token_economy = TokenEconomy(default_model=self.model)
        self.max_repair_attempts = 1
        # Sinal visível de falha de geração estruturada (FEAT-002).
        # None = última chamada de generate_structured bem-sucedida.
        # str  = descrição da última falha (LLM vazio/sem JSON/erro de rede).
        self.last_failure: str | None = None
        self._retry_config = RetryConfig(
            max_attempts=4,
            initial_wait_seconds=1.0,
            max_wait_seconds=15.0,
            retry_on=_retryable_exceptions_for(self.provider),
        )

    def _init_providers_safely(self) -> None:
        import importlib

        PROVIDER_MAP = [
            ("openrouter", "openai", "api_key"),
            ("anthropic", "anthropic", "api_key"),
            ("gemini", "google.genai", "api_key"),
            ("groq", "groq", "api_key"),
            ("ollama", "openai", None),
        ]
        for name, module_name, key_attr in PROVIDER_MAP:
            if key_attr and not self.config.get(key_attr):
                fallback_cfg = self._fallback_configs.get(name)
                if not fallback_cfg or not fallback_cfg.get(key_attr):
                    continue
            try:
                importlib.import_module(module_name)
                logger.debug(f"Provider {name} disponivel")
            except ImportError as e:
                logger.warning(f"Provider {name} indisponivel: {e}")
            except Exception as e:
                logger.error(f"Provider {name} falhou: {e}")

    def _rotate_key(self) -> bool:
        """Rotaciona a chave de API ativa do provedor para a próxima na lista.
        Retorna True se a rotação ocorreu (há chaves adicionais disponíveis)."""
        if len(self._api_keys) <= 1:
            return False
        self._current_key_idx = (self._current_key_idx + 1) % len(self._api_keys)
        new_key = self._api_keys[self._current_key_idx]
        self.config["api_key"] = new_key
        logger.warning(
            f"[RotaçãoChaves] Falha na chave anterior. Rotacionando para chave "
            f"{self._current_key_idx + 1}/{len(self._api_keys)} do provedor '{self.provider.value}'."
        )
        self._init_client()
        return True

    def _rotate_model(self) -> bool:
        """Rotaciona o modelo ativo para o próximo na lista de modelos.
        Retorna True se a rotação ocorreu (há modelos adicionais disponíveis)."""
        if len(self._models) <= 1:
            return False
        self._current_model_idx = (self._current_model_idx + 1) % len(self._models)
        self.model = self._models[self._current_model_idx]
        logger.warning(
            f"[RotaçãoModelos] Falha no modelo anterior. Rotacionando para modelo: '{self.model}' "
            f"({self._current_model_idx + 1}/{len(self._models)})."
        )
        # O cliente OpenAI não precisa ser reiniciado pois passamos o model na chamada,
        # mas para provedores como Gemini, Anthropic ou Ollama que amarram o model
        # na inicialização do SDK client, nós re-inicializamos o client.
        self._init_client()
        return True

    # ── Inicialização ─────────────────────────────────────────────────────────

    def _init_client(self):
        if self.provider == LLMProvider.ANTHROPIC:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.config.get("api_key"))
            self.model = self.config.get("model", "claude-sonnet-4-20250514")

        elif self.provider == LLMProvider.OPENAI:
            import httpx
            import openai

            self._client = openai.AsyncOpenAI(
                api_key=self.config.get("api_key"),
                http_client=httpx.AsyncClient(http2=False),
            )
            self.model = self.config.get("model", "gpt-4.1")

        elif self.provider == LLMProvider.OPENROUTER:
            import httpx
            import openai

            self._client = openai.AsyncOpenAI(
                api_key=self.config.get("api_key"),
                base_url="https://openrouter.ai/api/v1",
                http_client=httpx.AsyncClient(http2=False),
            )
            self.model = self.config.get("model", "google/gemma-4-26b-a4b-it:free")

        elif self.provider == LLMProvider.GROQ:
            try:
                import httpx
                from groq import AsyncGroq

                self._client = AsyncGroq(
                    api_key=self.config.get("api_key"),
                    http_client=httpx.AsyncClient(http2=False),
                )
            except ImportError:
                logger.warning("groq SDK não instalado. Groq indisponível.")
                self._client = None
            self.model = self.config.get("model", "llama-3.3-70b-versatile")

        elif self.provider == LLMProvider.OLLAMA:
            import httpx
            import openai

            self._client = openai.AsyncOpenAI(
                base_url=f"{self.config.get('base_url', 'http://localhost:11434')}/v1",
                api_key=self.config.get("api_key") or "ollama-local",
                http_client=httpx.AsyncClient(http2=False),
            )
            self.model = self.config.get("model", "llama3.1")

        elif self.provider == LLMProvider.GEMINI:
            try:
                from google import genai

                self._client = genai.Client(api_key=self.config.get("api_key"))
            except ImportError:
                logger.warning("google-genai não instalado. Gemini indisponível.")
                self._client = None
            self.model = self.config.get("model", "gemini-2.5-flash")

        elif self.provider == LLMProvider.DEEPSEEK:
            import httpx
            import openai

            self._client = openai.AsyncOpenAI(
                api_key=self.config.get("api_key"),
                base_url=self.config.get("base_url", "https://api.deepseek.com/v1"),
                http_client=httpx.AsyncClient(http2=False),
            )
            self.model = self.config.get("model", "deepseek-chat")

        elif self.provider == LLMProvider.GITHUB_MODELS:
            import httpx
            import openai

            self._client = openai.AsyncOpenAI(
                api_key=self.config.get("api_key"),
                base_url=self.config.get(
                    "base_url", "https://models.inference.ai.azure.com"
                ),
                http_client=httpx.AsyncClient(http2=False),
            )
            self.model = self.config.get("model", "gpt-4o-mini")

        else:
            raise ValueError(f"Provider não suportado: {self.provider}")

    # ── Geração principal (com failover automático) ───────────────────────────

    async def generate(
        self, prompt: str, temperature: float = 0.3, max_tokens: int = 4000
    ) -> str:
        """Tenta o provider atual com retry de backoff; em caso de rate-limit persistente, aciona failover.

        Assinatura mantida idêntica à original por compatibilidade com callers
        existentes (report_generator, knowledge_graph, debate_orchestrator,
        etc.) e com testes que mockam `generate` diretamente. Quando chamada
        através de `complete()`, esta chamada já ocorre aninhada dentro de um
        span `trace_llm_call` com o `task_type` correto (ver `complete()`);
        aqui é criado apenas um span genérico, cobrindo também chamadas diretas.
        """
        last_exc: Exception | None = None
        wait = RATE_LIMIT_INITIAL_WAIT_S

        async with trace_llm_call(self.provider.value, self.model, "generic") as span:
            for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 1):
                try:
                    result = await self._generate_raw(prompt, temperature, max_tokens)
                    if span is not None:
                        try:
                            span.set_attribute("sra.llm.attempt", attempt)
                            span.set_attribute("gen_ai.response.length", len(result))
                        except Exception:
                            pass
                    return result
                except Exception as exc:
                    if _is_daily_quota(exc):
                        logger.warning(
                            f"[QuotaDiária] {self.provider.value} esgotou cota diária. "
                            "Acionando cadeia de failover imediatamente..."
                        )
                        break
                    elif _is_rate_limit(exc):
                        last_exc = exc
                        if attempt < RATE_LIMIT_MAX_RETRIES:
                            logger.warning(
                                f"[RateLimit] {self.provider.value} — tentativa {attempt}/{RATE_LIMIT_MAX_RETRIES}. "
                                f"Aguardando {wait:.0f}s antes de retry..."
                            )
                            await asyncio.sleep(wait)
                            wait *= 2
                        else:
                            logger.warning(
                                f"[Failover] {self.provider.value} esgotou {RATE_LIMIT_MAX_RETRIES} tentativas. "
                                "Acionando cadeia de failover..."
                            )
                    else:
                        raise

            # Todos os retries esgotados — acionar failover
            if span is not None:
                try:
                    span.set_attribute("sra.llm.failover_triggered", True)
                except Exception:
                    pass
            return await self._failover_generate(
                prompt, temperature, max_tokens, skip=self.provider
            )

    async def _generate_raw(
        self, prompt: str, temperature: float, max_tokens: int
    ) -> str:
        """Chamada direta ao provider atual com suporte a rotação automática de chaves."""
        attempts = max(1, len(self._api_keys))
        for attempt in range(attempts):
            try:
                if self.provider == LLMProvider.ANTHROPIC:
                    response = await self._client.messages.create(
                        model=self.model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    return response.content[0].text

                elif self.provider in (
                    LLMProvider.OPENAI,
                    LLMProvider.OPENROUTER,
                    LLMProvider.OLLAMA,
                ):
                    response = await self._client.chat.completions.create(
                        model=self.model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        messages=[{"role": "user", "content": prompt}],
                        timeout=30.0,
                    )
                    return response.choices[0].message.content

                elif self.provider == LLMProvider.GROQ:
                    if not self._client:
                        raise RuntimeError("Groq SDK não instalado")
                    response = await self._client.chat.completions.create(
                        model=self.model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        messages=[{"role": "user", "content": prompt}],
                        timeout=30.0,
                    )
                    return response.choices[0].message.content

                elif self.provider == LLMProvider.GEMINI:
                    if not self._client:
                        raise RuntimeError("Gemini SDK não instalado")
                    from google.genai import types as genai_types

                    coro = self._client.aio.models.generate_content(
                        model=self.model,
                        contents=prompt,
                        config=genai_types.GenerateContentConfig(
                            temperature=temperature,
                            max_output_tokens=max_tokens,
                        ),
                    )
                    response = await asyncio.wait_for(coro, timeout=30.0)
                    return response.text or ""
            except Exception as exc:
                if not isinstance(exc, (TypeError, ValueError, NameError, KeyError)):
                    if self._rotate_key():
                        continue
                    if self._rotate_model():
                        # Ao mudar o modelo, resetamos as chaves para tentar novamente do início
                        self._current_key_idx = 0
                        if self._api_keys:
                            self.config["api_key"] = self._api_keys[0]
                        self._init_client()
                        continue
                raise
        return ""

    async def _failover_generate(
        self,
        prompt: str,
        temperature: float,
        max_tokens: int,
        skip: LLMProvider,
    ) -> str:
        """
        Percorre a cadeia de fallback em ordem de preferência:
          openrouter → gemini → groq → ollama
        Pula o provider que já falhou (skip).
        """
        chain: list[str] = [
            "openrouter",
            "gemini",
            "groq",
            "deepseek",
            "github_models",
            "ollama",
        ]

        for provider_name in chain:
            if provider_name == skip.value:
                continue
            cfg = self._fallback_configs.get(provider_name)
            if not cfg:
                logger.debug(f"[Failover] Sem config para '{provider_name}', pulando.")
                continue

            try:
                provider_enum = LLMProvider(provider_name)
                logger.info(f"[Failover] Tentando provider: {provider_name}")
                async with trace_llm_call(
                    provider_name, cfg.get("model", "?"), task_type="failover"
                ):
                    result = await self._call_provider(
                        provider_enum, cfg, prompt, temperature, max_tokens
                    )
                logger.info(f"[Failover] Sucesso com provider: {provider_name}")
                return result
            except Exception as exc:
                if _is_rate_limit(exc):
                    logger.warning(
                        f"[Failover] {provider_name} também com rate-limit: {exc}. Continuando..."
                    )
                else:
                    logger.warning(
                        f"[Failover] {provider_name} falhou: {exc}. Continuando..."
                    )

        logger.error("[Failover] Todos os providers falharam. Retornando string vazia.")
        return ""

    async def _call_provider(
        self,
        provider: LLMProvider,
        cfg: dict[str, Any],
        prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Executa a chamada ao provider de fallback com suporte a rotação de chaves de fallback."""
        api_key_raw = cfg.get("api_key") or ""
        if isinstance(api_key_raw, str) and "," in api_key_raw:
            keys = [k.strip() for k in api_key_raw.split(",") if k.strip()]
        else:
            keys = [api_key_raw] if api_key_raw else []

        last_exc = None
        for idx, key in enumerate(keys):
            cfg_copy = cfg.copy()
            cfg_copy["api_key"] = key
            try:
                if len(keys) > 1:
                    logger.info(
                        f"[RotaçãoChaves] Tentando chave {idx + 1}/{len(keys)} para fallback '{provider.value}'."
                    )
                return await self._call_provider_raw(
                    provider, cfg_copy, prompt, temperature, max_tokens
                )
            except Exception as exc:
                if (
                    not isinstance(exc, (TypeError, ValueError, NameError, KeyError))
                    and len(keys) > 1
                ):
                    logger.warning(
                        f"[RotaçãoChaves] Falha na chave {idx + 1}/{len(keys)} do fallback '{provider.value}': {exc}."
                    )
                    last_exc = exc
                    continue
                raise
        if last_exc:
            raise last_exc
        return ""

    async def _call_provider_raw(
        self,
        provider: LLMProvider,
        cfg: dict[str, Any],
        prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Cria um client temporário para o provider de fallback e executa a chamada crua."""
        if provider in (LLMProvider.OPENROUTER, LLMProvider.OPENAI, LLMProvider.OLLAMA):
            import openai

            if provider == LLMProvider.OPENROUTER:
                client = openai.AsyncOpenAI(
                    api_key=cfg["api_key"],
                    base_url="https://openrouter.ai/api/v1",
                )
                model = cfg.get("model", "google/gemma-4-26b-a4b-it:free")
            elif provider == LLMProvider.OLLAMA:
                client = openai.AsyncOpenAI(
                    base_url=f"{cfg.get('base_url', 'http://localhost:11434')}/v1",
                    api_key=cfg.get("api_key") or "ollama-local",
                )
                model = cfg.get("model", "llama3.1")
            else:
                client = openai.AsyncOpenAI(api_key=cfg["api_key"])
                model = cfg.get("model", "gpt-4.1")
            resp = await client.chat.completions.create(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                timeout=30.0,
            )
            return resp.choices[0].message.content

        elif provider == LLMProvider.GROQ:
            from groq import AsyncGroq

            client = AsyncGroq(api_key=cfg["api_key"])
            model = cfg.get("model", "llama-3.3-70b-versatile")
            resp = await client.chat.completions.create(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                timeout=30.0,
            )
            return resp.choices[0].message.content

        elif provider == LLMProvider.GEMINI:
            from google import genai
            from google.genai import types as genai_types

            client = genai.Client(api_key=cfg["api_key"])
            model = cfg.get("model", "gemini-2.5-flash")
            resp = await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )
            return resp.text or ""

        elif provider == LLMProvider.DEEPSEEK:
            import openai

            client = openai.AsyncOpenAI(
                api_key=cfg["api_key"],
                base_url=cfg.get("base_url", "https://api.deepseek.com/v1"),
            )
            model = cfg.get("model", "deepseek-chat")
            resp = await client.chat.completions.create(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                timeout=30.0,
            )
            return resp.choices[0].message.content

        elif provider == LLMProvider.GITHUB_MODELS:
            import openai

            client = openai.AsyncOpenAI(
                api_key=cfg["api_key"],
                base_url=cfg.get("base_url", "https://models.inference.ai.azure.com"),
            )
            model = cfg.get("model", "gpt-4o-mini")
            resp = await client.chat.completions.create(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                timeout=30.0,
            )
            return resp.choices[0].message.content

        return ""

    # ── Geração estruturada (JSON) ────────────────────────────────────────────

    @staticmethod
    def _extract_json_blob(text: str) -> str | None:
        """Extrai um fragmento JSON (objeto ou array) de um texto possivelmente sujo.

        Remove cercas ```json/```, e se ainda não for JSON puro, tenta
        capturar o primeiro bloco delimitado por ``[...]`` ou ``{...}`` via
        regex (com suporte a aninhamento superficial). Retorna ``None`` se
        nenhum JSON viável for encontrado.
        """
        if not text:
            return None
        candidate = text.strip()
        for fence in ("```json", "```"):
            if candidate.startswith(fence):
                candidate = candidate[len(fence) :]
        if candidate.endswith("```"):
            candidate = candidate[: -len("```")]
        candidate = candidate.strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass
        # Tenta extrair array/objeto do meio de texto solto
        for pattern in (r"\[.*\]", r"\{.*\}"):
            match = re.search(pattern, candidate, re.DOTALL)
            if match:
                blob = match.group(0).strip()
                try:
                    json.loads(blob)
                    return blob
                except json.JSONDecodeError:
                    # Último recurso: repara defeitos comuns de LLMs locais
                    # (vírgulas à direita, aspas tipográficas) e tenta de novo.
                    repaired = LLMClient._repair_json_defects(blob)
                    if repaired != blob:
                        try:
                            json.loads(repaired)
                            return repaired
                        except json.JSONDecodeError:
                            pass
                    continue
        return None

    @staticmethod
    def _repair_json_defects(blob: str) -> str:
        """Corrige defeitos sintáticos frequentes em JSON de LLMs locais.

        Repara: (1) aspas tipográficas “ ” ‘ ’ → aspas ASCII; (2) vírgulas
        à direita antes de ``]`` ou ``}``. Não tenta reparar JSON truncado
        (isso é deixado para o retry com prompt de reparo em
        ``generate_structured``). Retorna a string possivelmente modificada.
        """
        if not blob:
            return blob
        repaired = (
            blob.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        )
        # Remove vírgulas à direita: ", ]" → " ]" e ", }" → " }"
        repaired = re.sub(r",\s*([\]}])", r"\1", repaired)
        return repaired

    @staticmethod
    def _safe_parse_json(text: str) -> Any:
        """Tenta parsear JSON de uma string, com fallback para extração de bloco.

        Retorna o objeto Python decodificado ou ``None`` se impossível.
        Usa ``_extract_json_blob`` para tolerar markdown/ruído.
        """
        if not text:
            return None
        # Tenta direto
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
        # Tenta extrair bloco JSON
        blob = LLMClient._extract_json_blob(text)
        if blob is not None:
            try:
                return json.loads(blob)
            except json.JSONDecodeError:
                return None
        return None

    async def generate_structured(
        self, prompt: str, schema: dict[str, Any], temperature: float = 0.1
    ) -> Any:
        """Gera saída estruturada em JSON, resiliente a respostas não-JSON.

        Estratégia (ver GAP 3 do PLANO_FECHAR_GAPS.md):
        1. Solicita JSON puro ao modelo.
        2. Extrai o blob JSON mesmo se houver markdown/ruído (``_extract_json_blob``).
        3. Em falha de parse, faz 1 retry com prompt de reparo.
        4. Em falha definitiva, retorna um fallback seguro
           (``[]`` para schema ``array``, ``{}`` para schema ``object``)
           em vez de estourar ``JSONDecodeError`` para o caller.

        Args:
            prompt: Instrução para o modelo.
            schema: Esquema JSON esperado (``{"type": "array" | "object", ...}``).
            temperature: Temperatura de amostragem.

        Returns:
            Objeto Python decodificado do JSON (list/dict), ou fallback seguro.
        """
        is_array = schema.get("type") == "array"
        safe_fallback: Any = [] if is_array else {}

        json_prompt = (
            prompt
            + "\n\nResponda APENAS em JSON valido seguindo este schema: "
            + json.dumps(schema, ensure_ascii=False)
            + "\nNao inclua markdown, apenas JSON puro."
        )

        last_response: str = ""
        for attempt in range(self.max_repair_attempts + 1):
            if attempt == 0:
                instruction = json_prompt
            else:
                instruction = (
                    f"{json_prompt}\n\nSua resposta anterior NAO foi um JSON valido. "
                    f"Responda APENAS o JSON puro, sem texto adicional nem markdown."
                )
            try:
                response = await self.generate(instruction, temperature=temperature)
            except Exception as exc:
                logger.warning(f"generate_structured: falha de rede/LLM: {exc}")
                self.last_failure = f"erro de rede/LLM: {exc}"
                return safe_fallback
            last_response = response or ""
            parsed = self._safe_parse_json(last_response)
            if parsed is not None:
                self.last_failure = None
                return parsed
            else:
                logger.warning(
                    f"generate_structured: nenhum JSON válido encontrado (tentativa {attempt + 1})"
                )

        logger.error(
            "generate_structured: falha definitiva ao obter JSON válido. "
            "Retornando fallback seguro."
        )
        self.last_failure = (
            "falha definitiva ao obter JSON válido; última resposta vazia/sem JSON"
        )
        return safe_fallback

    # ── Geração estruturada nativa Pydantic (com Tenacity e Reparo) ──────────

    async def complete_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ModelT],
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> ModelT:
        """Chamada estruturada de alto nível que retorna uma instância validada de `response_model`."""
        from src.utils.retry import with_retry

        retrying_call = with_retry(self._retry_config)(self._call_provider_structured)

        last_error: ValidationError | None = None
        for repair_attempt in range(self.max_repair_attempts + 1):
            prompt = user_prompt
            if last_error is not None:
                prompt = (
                    f"{user_prompt}\n\n"
                    f"A resposta anterior não bateu com o schema esperado. "
                    f"Erro de validação: {last_error}\n"
                    f"Corrija e responda novamente seguindo exatamente o schema."
                )

            try:
                async with trace_llm_call(
                    self.provider.value,
                    self.model,
                    f"structured_{response_model.__name__.lower()}",
                ):
                    raw_json = await retrying_call(
                        system_prompt=system_prompt,
                        user_prompt=prompt,
                        response_model=response_model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                return response_model.model_validate_json(raw_json)
            except ValidationError as exc:
                last_error = exc
                logger.warning(
                    f"structured_output_validation_failed attempt={repair_attempt + 1}/{self.max_repair_attempts + 1} error={exc}"
                )
            except Exception as exc:
                logger.error(f"Erro na chamada estruturada do provider: {exc}")
                raise LLMClientError(f"Erro na chamada estruturada: {exc}") from exc

        raise OutputValidationError(
            f"Não foi possível validar a saída do modelo contra "
            f"{response_model.__name__} após {self.max_repair_attempts + 1} tentativa(s). "
            f"Último erro: {last_error}"
        )

    async def _call_provider_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ModelT],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Faz a chamada crua ao provider ativo usando structured outputs nativos com suporte a rotação."""
        attempts = max(1, len(self._api_keys))
        for attempt in range(attempts):
            try:
                if self.provider in (
                    LLMProvider.OPENAI,
                    LLMProvider.OPENROUTER,
                    LLMProvider.OLLAMA,
                ):
                    return await self._call_openai_structured(
                        system_prompt,
                        user_prompt,
                        response_model,
                        temperature,
                        max_tokens,
                    )
                elif self.provider == LLMProvider.ANTHROPIC:
                    return await self._call_anthropic_structured(
                        system_prompt,
                        user_prompt,
                        response_model,
                        temperature,
                        max_tokens,
                    )
                elif self.provider == LLMProvider.GEMINI:
                    return await self._call_gemini_structured(
                        system_prompt,
                        user_prompt,
                        response_model,
                        temperature,
                        max_tokens,
                    )
                elif self.provider == LLMProvider.GROQ:
                    return await self._call_groq_structured(
                        system_prompt,
                        user_prompt,
                        response_model,
                        temperature,
                        max_tokens,
                    )
                else:
                    json_prompt = (
                        f"{system_prompt}\n\n{user_prompt}"
                        + "\n\nResponda APENAS em JSON valido seguindo este schema: "
                        + json.dumps(
                            response_model.model_json_schema(), ensure_ascii=False
                        )
                        + "\nNao inclua markdown, apenas JSON puro."
                    )
                    raw = await self.generate(
                        json_prompt, temperature=temperature, max_tokens=max_tokens
                    )
                    raw = raw.strip()
                    for fence in ("```json", "```"):
                        if raw.startswith(fence):
                            raw = raw[len(fence) :]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    return raw.strip()
            except Exception as exc:
                if not isinstance(exc, (TypeError, ValueError, NameError, KeyError)):
                    if self._rotate_key():
                        continue
                    if self._rotate_model():
                        # Ao mudar o modelo, resetamos as chaves para tentar novamente do início
                        self._current_key_idx = 0
                        if self._api_keys:
                            self.config["api_key"] = self._api_keys[0]
                        self._init_client()
                        continue
                raise
        return ""

    async def _call_openai_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ModelT],
        temperature: float,
        max_tokens: int,
    ) -> str:
        completion = await self._client.beta.chat.completions.parse(
            model=self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=response_model,
        )
        choice = completion.choices[0]
        if getattr(choice.message, "refusal", None):
            raise LLMClientError(f"Modelo recusou a resposta: {choice.message.refusal}")
        return choice.message.content

    async def _call_anthropic_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ModelT],
        temperature: float,
        max_tokens: int,
    ) -> str:
        tool_name = f"emit_{response_model.__name__.lower()}"
        message = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[
                {
                    "name": tool_name,
                    "description": f"Emite a resposta estruturada conforme o schema {response_model.__name__}.",
                    "input_schema": response_model.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
        )
        for block in message.content:
            if block.type == "tool_use" and block.name == tool_name:
                return json.dumps(block.input)
        raise LLMClientError(
            "Anthropic não retornou um tool_use bloco com a resposta estruturada esperada."
        )

    async def _call_gemini_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ModelT],
        temperature: float,
        max_tokens: int,
    ) -> str:
        from google.genai import types as genai_types

        combined_prompt = f"{system_prompt}\n\n{user_prompt}"
        coro = self._client.aio.models.generate_content(
            model=self.model,
            contents=combined_prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=response_model,
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        response = await asyncio.wait_for(coro, timeout=30.0)
        return response.text or ""

    async def _call_groq_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ModelT],
        temperature: float,
        max_tokens: int,
    ) -> str:
        json_prompt = (
            f"{system_prompt}\n\n{user_prompt}"
            + "\n\nResponda APENAS em JSON valido seguindo este schema: "
            + json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        )
        resp = await self._client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": json_prompt}],
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content

    # ── Completion de alto nível (com SmartModelRouter) ───────────────────────

    async def complete(
        self,
        prompt: str,
        task_type: str = "synthesis",
        model_override: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4000,
    ) -> str:
        """
        High-level completion que seleciona o modelo via SmartModelRouter.
        Inclui failover automático e retry com backoff em caso de rate-limit.
        """
        target_model: str = model_override or self.model
        if not model_override and self.model_router is not None:
            provider_name = self.provider.value
            decision = self.model_router.route(task_type, provider_name)
            # Extrai o model_id do RoutingDecision (string), não o objeto inteiro
            target_model = (
                decision.model_id if hasattr(decision, "model_id") else str(decision)
            )
            logger.debug(
                f"SmartModelRouter: task={task_type} → model={target_model} "
                f"tier={getattr(decision, 'tier', '?')} score={getattr(decision, 'score', '?')}"
            )

        # Verifica budget
        if not self.token_economy.check_budget(prompt, target_model):
            logger.warning(
                f"TokenEconomy: Budget excedido para modelo {target_model}. Usando fallback Groq/Ollama."
            )
            return await self._failover_generate(
                prompt, temperature, max_tokens, skip=LLMProvider("__none__")
            )

        # Aplica truncamento inteligente
        truncated_prompt = self.token_economy.smart_truncate(
            prompt, max_tokens=max_tokens, model=target_model
        )

        response = ""
        original_model = self.model
        self.model = target_model
        try:
            # Span externo com o task_type real da etapa do pipeline (intent,
            # synthesis, report, etc.), aninhando o span genérico criado
            # internamente por `generate()` (que cobre tentativas/backoff).
            async with trace_llm_call(self.provider.value, target_model, task_type):
                response = await self.generate(
                    truncated_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
        finally:
            self.model = original_model
        logger.debug(
            f"LLMClient.complete: task={task_type} model={target_model} "
            f"chars_in={len(truncated_prompt)} chars_out={len(response)}"
        )

        # Registra uso de tokens
        input_tokens = self.token_economy.count_tokens(truncated_prompt, target_model)
        output_tokens = self.token_economy.count_tokens(response, target_model)
        self.token_economy.record_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=target_model,
            query_hint=task_type,
        )

        return response

    async def complete_stream(
        self,
        prompt: str,
        task_type: str = "synthesis",
        model_override: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4000,
    ):
        """
        Gerador assíncrono que faz streaming real quando o provider suporta,
        e cai em chunks simulados (sem sleep artificial) como fallback seguro.

        Compatível com SSE via FastAPI StreamingResponse.
        """
        # Providers que suportam streaming nativo via openai SDK
        if self.provider in (
            LLMProvider.OPENAI,
            LLMProvider.OPENROUTER,
            LLMProvider.OLLAMA,
            LLMProvider.GROQ,
        ):
            try:
                client = self._client
                if self.provider == LLMProvider.GROQ and not client:
                    raise RuntimeError("Groq SDK não instalado")
                stream = await client.chat.completions.create(
                    model=model_override or self.model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                )
                async for chunk in stream:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield delta
                return
            except Exception as exc:
                logger.warning(
                    f"[complete_stream] Streaming nativo falhou: {exc}. Usando fallback chunked."
                )

        # Anthropic streaming
        if self.provider == LLMProvider.ANTHROPIC:
            try:
                async with self._client.messages.stream(
                    model=model_override or self.model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    async for text in stream.text_stream:
                        yield text
                return
            except Exception as exc:
                logger.warning(
                    f"[complete_stream] Anthropic stream falhou: {exc}. Usando fallback chunked."
                )

        # Gemini streaming
        if self.provider == LLMProvider.GEMINI and self._client:
            try:
                from google.genai import types as genai_types

                response = await self._client.aio.models.generate_content(
                    model=model_override or self.model,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                    ),
                )
                text = response.text or ""
                # Gemini não tem streaming por chunk na SDK atual — entrega em blocos
                chunk_size = 80
                for i in range(0, len(text), chunk_size):
                    yield text[i : i + chunk_size]
                return
            except Exception as exc:
                logger.warning(
                    f"[complete_stream] Gemini stream falhou: {exc}. Usando fallback chunked."
                )

        # Fallback universal: complete() normal, chunked sem sleep
        full_response = await self.complete(
            prompt,
            task_type=task_type,
            model_override=model_override,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        chunk_size = 80
        for i in range(0, len(full_response), chunk_size):
            yield full_response[i : i + chunk_size]

    async def _fallback_to_ollama(
        self, prompt: str, temperature: float, max_tokens: int
    ) -> str:
        try:
            import openai

            client = openai.AsyncOpenAI(
                base_url="http://host.docker.internal:11434/v1",
                api_key="ollama-local",
            )
            response = await client.chat.completions.create(
                model="llama3.1",
                temperature=temperature,
                max_tokens=min(max_tokens, 2048),
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"Fallback Ollama falhou: {e}")
            return f"[Erro: Budget excedido e fallback local indisponível. Erro: {e}]"
