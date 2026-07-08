"""
vision_analyzer.py — Análise de Visão (Vision LLMs) — Fase 2.

Captura screenshots de páginas ricas em dados (via Playwright) e envia a um
modelo multimodal (Vision LLM) para extrair insights de diagramas de
arquitetura, gráficos de benchmark e tabelas de papers.

Design:
  - `VisionAnalyzer` é desacoplado do provider HTTP. Ele recebe uma
    ``vision_fn`` injetável ``(prompt, image_b64, mime) -> str`` (síncrona ou
    assíncrona) que faz a chamada ao modelo de visão. Isso mantém a classe
    100% testável (basta injetar um mock) e reutilizável com qualquer cliente.
  - Quando um ``llm_client`` OpenAI-compatível (OpenAI/OpenRouter/Ollama/Groq/
    DeepSeek/GitHub Models) é fornecido, uma ``vision_fn`` padrão é construída
    automaticamente a partir de ``llm_client._client`` e ``llm_client.model``.
  - A captura de tela usa Playwright (lazy init) e degrada graciosamente se o
    Playwright/browser não estiver disponível no host.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("vision_analyzer")

# Provider/SDK OpenAI-compatible (compartilham a mesma interface chat.completions)
_OPENAI_COMPATIBLE = {"openai", "openrouter", "ollama", "groq", "deepseek", "github_models"}

# Timeout de captura de tela em segundos
_SCREENSHOT_TIMEOUT_S = 45.0


async def _noop_capture() -> None:  # pragma: no cover - helper
    return None


class VisionAnalyzer:
    """Captura screenshots e analisa com um modelo de visão (LLM multimodal)."""

    def __init__(
        self,
        vision_fn: Callable[[str, str, str], Any] | None = None,
        llm_client: Any = None,
        vision_model: str | None = None,
        screenshot_dir: str = "screenshots",
    ) -> None:
        """
        Args:
            vision_fn: Função ``(prompt, image_b64, mime) -> str`` (ou corrotina)
                que efetua a chamada ao modelo de visão. Se None, tenta construir
                a partir de ``llm_client`` (ver ``_build_default_vision_fn``).
            llm_client: Cliente LLM do SRA (espera atributos ``_client``,
                ``model`` e ``provider``). Usado apenas para derivar a
                ``vision_fn`` padrão quando ``vision_fn`` não é fornecida.
            vision_model: Modelo de visão explícito. Se None, usa
                ``llm_client.model``.
            screenshot_dir: Diretório onde os screenshots são salvos.
        """
        self.screenshot_dir = screenshot_dir
        os.makedirs(self.screenshot_dir, exist_ok=True)

        self._vision_fn = vision_fn or self._build_default_vision_fn(
            llm_client, vision_model
        )
        self._llm_client = llm_client
        self._playwright = None
        self._browser = None

    # ── Construção da vision_fn padrão ─────────────────────────────────────────

    @staticmethod
    def _build_default_vision_fn(
        llm_client: Any, vision_model: str | None
    ) -> Callable[[str, str, str], Any] | None:
        """Constrói uma vision_fn OpenAI-compatível a partir do LLMClient do SRA.

        Retorna None se o cliente não for OpenAI-compatível ou não tiver
        cliente HTTP inicializado (ex.: Gemini/Anthropic sem adapter).
        """
        if llm_client is None:
            return None
        provider = getattr(llm_client, "provider", None)
        provider_name = getattr(provider, "value", str(provider)).lower()
        http_client = getattr(llm_client, "_client", None)
        if provider_name not in _OPENAI_COMPATIBLE or http_client is None:
            logger.warning(
                "VisionAnalyzer: provider '%s' não suporta vision_fn padrão "
                "(OpenAI-compatible). Forneça vision_fn explicitamente.",
                provider_name,
            )
            return None

        model = vision_model or getattr(llm_client, "model", "gpt-4o")

        async def _openai_vision(prompt: str, image_b64: str, mime: str) -> str:
            response = await http_client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime};base64,{image_b64}"
                                },
                            },
                        ],
                    }
                ],
                max_tokens=1500,
                temperature=0.2,
            )
            return response.choices[0].message.content or ""

        return _openai_vision

    # ── Captura de tela (Playwright) ──────────────────────────────────────────

    async def capture_screenshot(
        self, url: str, output_path: str | None = None
    ) -> str | None:
        """Captura um screenshot PNG da URL e retorna o caminho do arquivo.

        Args:
            url: URL da página a ser capturada.
            output_path: Caminho opcional. Se None, gera um arquivo temporário
                dentro de ``screenshot_dir``.

        Returns:
            Caminho do screenshot PNG ou None em caso de falha indisponibilidade.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning(
                "VisionAnalyzer: Playwright não instalado — screenshot indisponível."
            )
            return None

        if output_path is None:
            safe = (
                "".join(c if c.isalnum() else "_" for c in url)[:60]
                + ".png"
            )
            output_path = os.path.join(self.screenshot_dir, safe)

        try:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            if self._browser is None:
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            context = await self._browser.new_context(
                viewport={"width": 1280, "height": 1600}
            )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(1.0)  # estabiliza render de gráficos
                await page.screenshot(path=output_path, full_page=True)
            finally:
                await page.close()
                await context.close()
            logger.info("VisionAnalyzer: screenshot salvo em %s", output_path)
            return output_path
        except Exception as e:
            logger.warning("VisionAnalyzer: falha ao capturar '%s': %s", url, e)
            return None

    async def close(self) -> None:
        """Encerra o browser Playwright se foi inicializado."""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as e:  # pragma: no cover - defensivo
                logger.warning("VisionAnalyzer: erro ao fechar browser: %s", e)
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # pragma: no cover - defensivo
                pass
            self._playwright = None

    # ── Análise de imagem ──────────────────────────────────────────────────────

    @staticmethod
    def _encode_image(image_path: str) -> tuple[str, str]:
        """Lê a imagem e retorna (base64, mime_type)."""
        mime, _ = mimetypes.guess_type(image_path)
        if mime is None:
            mime = "image/png"
        with open(image_path, "rb") as fh:
            data = fh.read()
        return base64.b64encode(data).decode("ascii"), mime

    async def analyze_image(self, image_path: str, prompt: str) -> str:
        """Analisa uma imagem já existente no disco com o modelo de visão.

        Args:
            image_path: Caminho para o arquivo de imagem (PNG/JPG/etc.).
            prompt: Instrução de análise (ex.: "extraia os eixos e valores do
                gráfico de benchmark").

        Returns:
            Texto da análise ou mensagem de erro amigável.
        """
        if self._vision_fn is None:
            return (
                "VisionAnalyzer: nenhum modelo de visão configurado "
                "(forneça vision_fn ou llm_client OpenAI-compatível)."
            )
        if not image_path or not os.path.isfile(image_path):
            return f"VisionAnalyzer: arquivo de imagem não encontrado: {image_path}"

        try:
            image_b64, mime = self._encode_image(image_path)
        except Exception as e:
            logger.warning("VisionAnalyzer: falha ao codificar imagem: %s", e)
            return f"VisionAnalyzer: não foi possível ler a imagem: {e}"

        try:
            result = self._vision_fn(prompt, image_b64, mime)
            if asyncio.iscoroutine(result):
                result = await result
            return result or ""
        except Exception as e:
            logger.warning("VisionAnalyzer: falha na chamada de visão: %s", e)
            return f"VisionAnalyzer: erro na análise de visão: {e}"

    async def analyze_url(self, url: str, prompt: str) -> dict[str, Any]:
        """Captura a URL e analisa o screenshot resultante.

        Returns:
            Dicionário com ``screenshot`` (caminho ou None), ``analysis``
            (texto) e ``success`` (bool).
        """
        screenshot = await self.capture_screenshot(url)
        if not screenshot:
            return {
                "screenshot": None,
                "analysis": "Falha ao capturar screenshot da URL.",
                "success": False,
            }
        analysis = await self.analyze_image(screenshot, prompt)
        return {
            "screenshot": screenshot,
            "analysis": analysis,
            "success": analysis.strip() != ""
            and not analysis.startswith("VisionAnalyzer:"),
        }
