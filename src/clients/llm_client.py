import json
from typing import Dict, Any, Optional
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"
    OLLAMA = "ollama"


class LLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        config: Dict[str, Any],
        model_router=None,
    ):
        self.provider = provider
        self.config = config
        self._client = None
        self.model = ""
        self.model_router = model_router
        self._init_client()
        from src.token_economy import TokenEconomy
        self.token_economy = TokenEconomy(default_model=self.model)

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
            self.model = self.config.get("model", "anthropic/claude-sonnet-4")

        elif self.provider == LLMProvider.OLLAMA:
            import openai
            self._client = openai.AsyncOpenAI(
                base_url=f"{self.config.get('base_url', 'http://localhost:11434')}/v1",
                api_key="ollama",
            )
            self.model = self.config.get("model", "llama3.1")

        elif self.provider == LLMProvider.GEMINI:
            try:
                from google import genai
                self._client = genai.Client(api_key=self.config.get("api_key"))
            except ImportError:
                logger.warning("google-genai nao instalado. Gemini indisponivel.")
                self._client = None
            self.model = self.config.get("model", "gemini-2.5-flash")

        else:
            raise ValueError(f"Provider nao suportado: {self.provider}")

    async def generate(
        self, prompt: str, temperature: float = 0.3, max_tokens: int = 4000
    ) -> str:
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

        elif self.provider == LLMProvider.GEMINI:
            if not self._client:
                return ""
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

    async def generate_structured(
        self, prompt: str, schema: Dict[str, Any], temperature: float = 0.1
    ) -> Dict[str, Any]:
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

    async def complete(
        self,
        prompt: str,
        task_type: str = "synthesis",
        model_override: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4000,
    ) -> str:
        """
        High-level completion that selects the model automatically via ModelRouter.

        task_type drives model selection when model_router is available.
        model_override bypasses routing entirely when a specific model is required.
        Falls back to self.generate() when no router is attached.
        """
        # Determina o modelo que será usado
        target_model = model_override
        if not target_model:
            if self.model_router is not None:
                provider_name = self.provider.value
                target_model = self.model_router.route(task_type, provider_name)
            else:
                target_model = self.model

        # 1. Verifica budget ANTES de chamar
        if not self.token_economy.check_budget(prompt, target_model):
            logger.warning(f"TokenEconomy: Budget excedido para modelo {target_model}. Usando Ollama local.")
            return await self._fallback_to_ollama(prompt, temperature, max_tokens)

        # 2. Aplica smart_truncate se o prompt for muito longo
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
            logger.debug(
                f"LLMClient.complete: task={task_type} routed to {routed_model}"
            )
        else:
            response = await self.generate(truncated_prompt, temperature=temperature, max_tokens=max_tokens)

        # 3. Registra uso real de tokens
        input_tokens = self.token_economy.count_tokens(truncated_prompt, target_model)
        output_tokens = self.token_economy.count_tokens(response, target_model)
        self.token_economy.record_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=target_model,
            query_hint=task_type
        )

        return response

    async def _fallback_to_ollama(self, prompt: str, temperature: float, max_tokens: int) -> str:
        try:
            import openai
            client = openai.AsyncOpenAI(
                base_url="http://host.docker.internal:11434/v1",
                api_key="ollama"
            )
            response = await client.chat.completions.create(
                model="llama3.1",
                temperature=temperature,
                max_tokens=min(max_tokens, 2048),
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"Fallback Ollama falhou: {e}")
            return f"[Erro: Budget excedido e fallback local indisponível. Erro: {e}]"
