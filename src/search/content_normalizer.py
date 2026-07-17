"""
content_normalizer.py — Normalizador Web Unificado (FEAT-003).

Adaptado do ``tools/web_tools.py`` do Hermes Agent (Nous Research, MIT,
somente-leitura) para o SRA. Remove todo acoplamento a OpenRouter/Nous e
roteia o resumo através do ``LLMClient`` já existente no SRA (que usa o
``SmartModelRouter`` internamente).

Responsabilidades:
  1. ``normalize(raw)``: limpa HTML/markdown bruto em texto plano (strip de
     tags, colapso de whitespace, remoção de boilerplate comum).
  2. ``summarize(text, max_tokens, cost_optimization=False)``: gera um resumo
     curto via LLM para reduzir o consumo de tokens no deep research. Quando
     ``cost_optimization=True`` NÃO chama o LLM (retorna o texto truncado),
     poupando custo em modos de economia. Em falha de LLM degrade para o
     texto truncado (nunca crash).

Segurança: toda saída passa por ``redact_sensitive_text`` (respeita
``SRA_REDACT_SECRETS``), mascarando segredos antes de persistir/expor.
"""

from __future__ import annotations

import html
import logging
import re

from src.logging_utils import redact_sensitive_text

logger = logging.getLogger(__name__)

# Limite de caracteres enviados ao LLM numa única chamada de resumo.
# Textos maiores são head+tail truncados antes de ir ao modelo.
DEFAULT_SUMMARY_CHAR_LIMIT: int = 12_000

# Fração preservada no cabeçalho quando o texto excede o limite de resumo.
_SUMMARY_HEAD_RATIO: float = 0.7

# Regex para tags HTML/XML (abre, fecha, auto-fechada, comentário, script, style).
_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_MULTI_WS_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_LEADING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)


def _strip_html_tags(raw: str) -> str:
    """Remove tags de script/style, comentários e demais tags HTML."""
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _COMMENT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    # Entidades HTML (&#39;, &amp;, &nbsp;...) → caracteres reais.
    text = html.unescape(text)
    return text


def _collapse_whitespace(text: str) -> str:
    """Colapsa espaços horizontais e linhas em branco excessivas."""
    text = _MULTI_WS_RE.sub(" ", text)
    text = _LEADING_WS_RE.sub("", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _head_tail_truncate(text: str, limit: int, head_ratio: float = 0.7) -> str:
    """Trunca preservando ``head_ratio`` do início e o resto do fim.

    Mantém o início (tese/cabeçalho) e o fim (conclusão) do conteúdo, que
    carregam a maior parte da informação útil para o deep research.
    """
    if len(text) <= limit:
        return text
    head_len = int(limit * head_ratio)
    tail_len = limit - head_len
    return text[:head_len] + "\n\n[... CONTEÚDO TRUNCADO ...]\n\n" + text[-tail_len:]


class ContentNormalizer:
    """Normaliza e resume conteúdo bruto de Firecrawl/SearXNG/Jina.

    O resumo é opcional e delegado a um ``LLMClient`` injetado. Sem cliente
    (ou com ``cost_optimization=True``) o normalizador atua apenas como
    limpador/truncador — sempre degrada graciosamente.
    """

    def __init__(
        self,
        llm_client=None,
        summary_char_limit: int = DEFAULT_SUMMARY_CHAR_LIMIT,
    ) -> None:
        """Inicializa o normalizador.

        Args:
            llm_client: Instância de ``LLMClient`` (opcional). Se None, o
                resumo nunca chama LLM e ``summarize`` retorna o texto
                truncado (modo offline).
            summary_char_limit: Teto de caracteres enviados por chamada de
                resumo ao LLM.
        """
        self.llm_client = llm_client
        self.summary_char_limit = summary_char_limit

    def normalize(self, raw: str) -> str:
        """Limpa conteúdo bruto (HTML/markdown) em texto plano.

        Args:
            raw: Conteúdo bruto retornado por um searcher (pode conter tags
                HTML, markdown, whitespace excessivo).

        Returns:
            Texto limpo. Strings vazias/nulas retornam ``""``. A saída é
            redactada (segredos mascarados) conforme ``SRA_REDACT_SECRETS``.
        """
        if not raw:
            return ""
        if not isinstance(raw, str):
            raw = str(raw)
        cleaned = _strip_html_tags(raw)
        cleaned = _collapse_whitespace(cleaned)
        return redact_sensitive_text(cleaned)

    async def summarize(
        self,
        text: str,
        max_tokens: int = 512,
        cost_optimization: bool = False,
    ) -> str:
        """Resume ``text`` via LLM para reduzir tokens no deep research.

        Comportamento:
          - ``cost_optimization=True`` → NÃO chama o LLM; retorna o texto
            truncado (economia de custo).
          - Sem ``llm_client`` → retorna o texto truncado (modo offline).
          - Falha de LLM/rede → retorna o texto truncado (degradação, loga).

        Args:
            text: Texto já normalizado (ou bruto) a resumir.
            max_tokens: Orçamento de saída do resumo (tokens).
            cost_optimization: Se True, desliga o resumo LLM.

        Returns:
            Resumo curto (ou texto truncado em modo de economia/falha).
            Sempre redactado antes de retornar.
        """
        if not text:
            return ""

        # Modo economia ou sem cliente → trunca e devolve (sem LLM).
        if cost_optimization or self.llm_client is None:
            truncated = _head_tail_truncate(
                text, self.summary_char_limit, _SUMMARY_HEAD_RATIO
            )
            return redact_sensitive_text(truncated)

        # Trunca antes de enviar ao LLM para respeitar o teto de custo.
        payload = _head_tail_truncate(
            text, self.summary_char_limit, _SUMMARY_HEAD_RATIO
        )

        prompt = (
            "Resuma o texto a seguir mantendo apenas os fatos, números e "
            "decisões técnicas essenciais para uma pesquisa. Seja denso e "
            f"sem redundância. Máximo de {max_tokens} tokens.\n\n"
            f"TEXTO:\n{payload}"
        )
        try:
            summary = await self.llm_client.complete(
                prompt,
                task_type="synthesis",
                temperature=0.2,
                max_tokens=max_tokens,
            )
            if not summary:
                logger.warning(
                    "ContentNormalizer: resumo LLM vazio; usando truncamento."
                )
                return redact_sensitive_text(payload)
            return redact_sensitive_text(summary.strip())
        except Exception as exc:
            logger.warning(
                "ContentNormalizer: falha no resumo LLM (%s); "
                "degradando para texto truncado.",
                exc,
            )
            return redact_sensitive_text(payload)
