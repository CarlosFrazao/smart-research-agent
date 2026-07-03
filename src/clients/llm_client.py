"""
LLMClient — cliente unificado com failover automático entre providers.

Ordem de failover ao receber 429 / 503 / RateLimitError:
  1. OpenRouter  (free tier)
  2. Gemini      (gemini-2.5-flash — free tier)
  3. Groq        (llama-3.3-70b-versatile — free tier)
  4. Ollama      (local — fallback final)

O provider ativo é determinado por LLM_PROVIDER no .env.
Se ele falhar com rate-limit, a cadeia acima é tentada automaticamente.
"""

import json
import logging
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

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


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"
    GROQ = "groq"
    OLLAMA = "ollama"


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
        self.config = config
        self._client = None
        self.model = ""
        self.model_router = model_router
        # fallback_configs: {"gemini": {...}, "groq": {...}, "openrouter": {...}, "ollama": {...}}
        self._fallback_configs: dict[str, dict[str, Any]] = fallback_configs or {}
        self._init_providers_safely()
        self._init_client()
        from src.token_economy import TokenEconomy
        self.token_economy = TokenEconomy(default_model=self.model)

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


    # ── Inicialização ─────────────────────────────────────────────────────────

    def _init_client(self):
        if self.provider == LLMProvider.ANTHROPIC:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self.config.get("api_key"))
            self.model = self.config.get("model", "claude-sonnet-4-20250514")

        elif self.provider == LLMProvider.OPENAI:
            import openai
            self._client = openai.AsyncOpenAI(api_key=self.config.get("api_key"))
            self.model = self.config.get("model", "gpt-4.1")

        elif self.provider == LLMProvider.OPENROUTER:
            import openai
            self._client = openai.AsyncOpenAI(
                api_key=self.config.get("api_key"),
                base_url="https://openrouter.ai/api/v1",
            )
            self.model = self.config.get("model", "google/gemma-4-26b-a4b-it:free")

        elif self.provider == LLMProvider.GROQ:
            try:
                from groq import AsyncGroq
                self._client = AsyncGroq(api_key=self.config.get("api_key"))
            except ImportError:
                logger.warning("groq SDK não instalado. Groq indisponível.")
                self._client = None
            self.model = self.config.get("model", "llama-3.3-70b-versatile")

        elif self.provider == LLMProvider.OLLAMA:
            import openai
            self._client = openai.AsyncOpenAI(
                base_url=f"{self.config.get('base_url', 'http://localhost:11434')}/v1",
                api_key=self.config.get("api_key") or "ollama-local",
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

        else:
            raise ValueError(f"Provider não suportado: {self.provider}")

    # ── Geração principal (com failover automático) ───────────────────────────

    async def generate(
        self, prompt: str, temperature: float = 0.3, max_tokens: int = 4000
    ) -> str:
        """Tenta o provider atual; em caso de rate-limit, percorre a cadeia de fallback."""
        try:
            return await self._generate_raw(prompt, temperature, max_tokens)
        except Exception as exc:
            if _is_rate_limit(exc):
                logger.warning(
                    f"[Failover] {self.provider.value} retornou rate-limit: {exc}. "
                    "Tentando próximo provider..."
                )
                return await self._failover_generate(prompt, temperature, max_tokens, skip=self.provider)
            raise

    async def _generate_raw(
        self, prompt: str, temperature: float, max_tokens: int
    ) -> str:
        """Chamada direta ao provider atual, sem lógica de failover."""
        if self.provider == LLMProvider.ANTHROPIC:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        elif self.provider in (LLMProvider.OPENAI, LLMProvider.OPENROUTER, LLMProvider.OLLAMA):
            response = await self._client.chat.completions.create(
                model=self.model,
                temperature=temperature,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
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
            )
            return response.choices[0].message.content

        elif self.provider == LLMProvider.GEMINI:
            if not self._client:
                raise RuntimeError("Gemini SDK não instalado")
            from google.genai import types as genai_types
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )
            return response.text or ""

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
        chain: list[str] = ["openrouter", "gemini", "groq", "ollama"]

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
                result = await self._call_provider(
                    provider_enum, cfg, prompt, temperature, max_tokens
                )
                logger.info(f"[Failover] Sucesso com provider: {provider_name}")
                return result
            except Exception as exc:
                if _is_rate_limit(exc):
                    logger.warning(f"[Failover] {provider_name} também com rate-limit: {exc}. Continuando...")
                else:
                    logger.warning(f"[Failover] {provider_name} falhou: {exc}. Continuando...")

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
        """Cria um client temporário para o provider de fallback e executa a chamada."""
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

        return ""

    # ── Geração estruturada (JSON) ────────────────────────────────────────────

    async def generate_structured(
        self, prompt: str, schema: dict[str, Any], temperature: float = 0.1
    ) -> dict[str, Any]:
        json_prompt = (
            prompt
            + "\n\nResponda APENAS em JSON valido seguindo este schema: "
            + json.dumps(schema, ensure_ascii=False)
            + "\nNao inclua markdown, apenas JSON puro."
        )
        response = await self.generate(json_prompt, temperature=temperature)

        response = response.strip()
        for fence in ("```json", "```"):
            if response.startswith(fence):
                response = response[len(fence):]
        if response.endswith("```"):
            response = response[:-3]
        response = response.strip()

        return json.loads(response)

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
        Inclui failover automático em caso de rate-limit.
        """
        target_model = model_override
        if not target_model:
            if self.model_router is not None:
                provider_name = self.provider.value
                target_model = self.model_router.route(task_type, provider_name)
            else:
                target_model = self.model

        # Verifica budget
        if not self.token_economy.check_budget(prompt, target_model):
            logger.warning(
                f"TokenEconomy: Budget excedido para modelo {target_model}. Usando fallback Groq/Ollama."
            )
            return await self._failover_generate(prompt, temperature, max_tokens, skip=LLMProvider("__none__"))

        # Aplica truncamento inteligente
        truncated_prompt = self.token_economy.smart_truncate(
            prompt, max_tokens=max_tokens, model=target_model
        )

        response = ""
        if model_override:
            original_model = self.model
            self.model = model_override
            try:
                response = await self.generate(truncated_prompt, temperature=temperature, max_tokens=max_tokens)
            finally:
                self.model = original_model
        elif self.model_router is not None:
            provider_name = self.provider.value
            routed_model = self.model_router.route(task_type, provider_name)
            original_model = self.model
            self.model = routed_model
            try:
                response = await self.generate(
                    truncated_prompt, temperature=temperature, max_tokens=max_tokens
                )
            finally:
                self.model = original_model
            logger.debug(f"LLMClient.complete: task={task_type} routed to {routed_model}")
        else:
            response = await self.generate(truncated_prompt, temperature=temperature, max_tokens=max_tokens)

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
        if self.provider in (LLMProvider.OPENAI, LLMProvider.OPENROUTER, LLMProvider.OLLAMA, LLMProvider.GROQ):
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
                logger.warning(f"[complete_stream] Streaming nativo falhou: {exc}. Usando fallback chunked.")

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
                logger.warning(f"[complete_stream] Anthropic stream falhou: {exc}. Usando fallback chunked.")

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
                logger.warning(f"[complete_stream] Gemini stream falhou: {exc}. Usando fallback chunked.")

        # Fallback universal: complete() normal, chunked sem sleep
        full_response = await self.complete(
            prompt, task_type=task_type, model_override=model_override,
            temperature=temperature, max_tokens=max_tokens
        )
        chunk_size = 80
        for i in range(0, len(full_response), chunk_size):
            yield full_response[i : i + chunk_size]



    async def _fallback_to_ollama(self, prompt: str, temperature: float, max_tokens: int) -> str:
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
