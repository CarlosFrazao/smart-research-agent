"""Filtro determinístico de prompt injection para conteúdo de fontes externas.

Este módulo fornece :class:`PromptSanitizer`, um pré-filtro **determinístico**
(baseado em regex compiladas) que remove padrões conhecidos de *prompt
injection* e *jailbreak* de conteúdo de fontes não-confiáveis **antes** de esse
conteúdo ser interpolado em prompts enviados ao LLM.

Ele é **complementar** (defense-in-depth) ao ``LLMSanitizer`` existente
(``src/security/llm_sanitizer.py``), que opera via chamada de LLM sobre
descrições longas (>100 chars) em ``search_stage.py``. O ``PromptSanitizer``:

* É determinístico e barato (regex, **sem** chamada de LLM) — seguro para
  rodar em qualquer ponto do pipeline, inclusive sobre títulos curtos e
  descrições que o ``LLMSanitizer`` atualmente ignora (<100 chars).
* Cobre os tokens de controle de LLM (``<|system|>``, ``[INST]``, ``<<SYS>>``,
  ``### System``) e as principais técnicas de jailbreak de 2025 (DAN,
  "do anything now", "developer mode", "ignore previous instructions", etc.).
* Foi desenhado para **baixo false-positive** em conteúdo legítimo de
  segurança/IA: os padrões exigem ancoragem contextual (ex.: "ignore previous
  *instructions*", não "ignore previous *errors*") e termos genéricos como
  "act as"/"aja como" foram **deliberadamente excluídos** por gerarem muitos
  falsos positivos.
* **Nunca** registra o conteúdo malicioso nos logs — apenas o *fingerprint*
  (hash SHA-256 do trecho) e o nome do padrão disparado.

O filtro substitui cada ocorrência por :data:`REMOVED_MARKER`, preservando o
restante do texto (degradação graciosa: falha de engine → conteúdo original).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Marcador substituto — preserva a estrutura do texto sem vazar o payload.
REMOVED_MARKER = "[REMOVED:INJECTION]"

# Padrões (nome, regex compilada). Compilados uma vez no import.
# IGNORECASE para cobrir variações de caixa; DOTALL para tokens multilinha.
_INJECTION_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # ── Tokens de controle de LLM (maior precisão: quase nunca legítimos) ──
    (
        "llm_control_token",
        re.compile(r"<\|(?:im_start|im_end|system|assistant|user)\|>", re.IGNORECASE),
    ),
    ("llm_inst_tag", re.compile(r"\[/?INST\]", re.IGNORECASE)),
    ("llm_sys_tag", re.compile(r"<<SYS>>", re.IGNORECASE)),
    # Cabeçalhos de instrução injetados (## System / ### Instruction / # User:)
    # Precisão: o cabeçalho deve ser EXATAMENTE system/instruction/assistant/user,
    # seguido opcionalmente de ':'/'-' e quebra de linha — "## System Requirements"
    # NÃO casa porque tem palavra seguinte após "system".
    (
        "instruction_header",
        re.compile(
            r"(?:^|\n)\s*#{1,3}\s*(?:system|instruction|assistant|user)\s*[:\-]?\s*(?:\n|$)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    # ── Override direto de instruções (EN) ──
    # Ancorado em palavras de instrução (instructions/prompt/context/messages) —
    # "ignore previous *errors*" NÃO casa, evitando falso positivo.
    (
        "ignore_instructions",
        re.compile(
            r"ignore\s+(?:all\s+)?(?:previous\s+|prior\s+|above\s+|the\s+above\s+|"
            r"the\s+previous\s+)?"
            r"(?:instructions?|prompts?|context|messages?|"
            r"the\s+(?:above|previous|system\s+prompt))",
            re.IGNORECASE,
        ),
    ),
    (
        "disregard_directive",
        re.compile(
            r"disregard\s+(?:all|previous|above|prior|the)\s+"
            r"(?:instructions?|prompt|context|messages?)",
            re.IGNORECASE,
        ),
    ),
    (
        "forget_directive",
        re.compile(
            r"forget\s+(?:everything|all|what|your|anything)\s+"
            r"(?:you\s+)?(?:know|knew|were\s+told|learned|trained|remember|said)",
            re.IGNORECASE,
        ),
    ),
    # Ancorado em papéis de modelo (assistant/chatbot/model/ai...) — "you are now
    # a *senior engineer*" NÃO casa.
    (
        "role_reassign",
        re.compile(
            r"you\s+are\s+now\s+(?:a|an|the)\s+"
            r"(?:assistant|chatbot|model|ai|language\s+model|llm|bot|gpt|"
            r"unfiltered|uncensored|unrestricted)",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_extract",
        re.compile(
            r"(?:repeat|reveal|print|show|output|dump|give\s+me)\s+"
            r"(?:your|the|me\s+the)\s+"
            r"(?:system\s+prompt|instructions?|initial\s+prompt|hidden\s+prompt)",
            re.IGNORECASE,
        ),
    ),
    # Ancorado em papel de modelo — "pretend to be *happy*" NÃO casa.
    (
        "pretend_directive",
        re.compile(
            r"pretend\s+(?:to\s+be|you\s+are|that\s+you\s+are)\s+(?:a|an)\s+"
            r"(?:assistant|chatbot|model|ai|language\s+model|llm|bot|gpt|"
            r"unfiltered|uncensored|unrestricted|jailbreak)",
            re.IGNORECASE,
        ),
    ),
    # ── Tokens de jailbreak conhecidos (2025) ──
    # "jailbreak" isolado foi EXCLUÍDO de propósito: aparece em prosa legítima
    # de segurança/IA ("how the latest jailbreak works") e geraria falso positivo.
    # Mantidos apenas tokens de altíssima precisão.
    (
        "jailbreak_token",
        re.compile(
            r"\b(?:DAN|do\s+anything\s+now|root\s+mode|god\s+mode)\b",
            re.IGNORECASE,
        ),
    ),
    # ── Override direto de instruções (PT-BR) ──
    (
        "ignore_instructions_pt",
        re.compile(
            r"ignore\s+(?:todas|todos|as\s+instruções|as\s+regras|o\s+contexto|"
            r"acima|anteriores)",
            re.IGNORECASE,
        ),
    ),
    (
        "forget_directive_pt",
        re.compile(
            r"esqueça\s+(?:tudo|tudo\s+que|o\s+que|suas\s+instruções)", re.IGNORECASE
        ),
    ),
    (
        "role_reassign_pt",
        re.compile(
            r"você\s+é\s+(?:agora\s+)?(?:um|uma|o|a)\s+"
            r"(?:assistente|chatbot|modelo|ia|llm)",
            re.IGNORECASE,
        ),
    ),
    (
        "pretend_directive_pt",
        re.compile(r"aja\s+como\s+se\s+você\s+fosse", re.IGNORECASE),
    ),
    (
        "system_prompt_extract_pt",
        re.compile(r"revele\s+(?:seu|o)\s+prompt(?:\s+de\s+sistema)?", re.IGNORECASE),
    ),
)


@dataclass
class SanitizationResult:
    """Resultado da sanitização determinística de um trecho de texto.

    Atributos:
        original: Texto antes da sanitização.
        cleaned: Texto após a remoção dos padrões de injection.
        was_injection_detected: ``True`` se algum padrão foi encontrado.
        matched_patterns: Lista com os **nomes** dos padrões disparados
            (nunca o conteúdo cru — ver :mod:`logging`).
        content_hash: SHA-256 do texto original, para correlação em logs/auditoria
            sem expor o conteúdo malicioso.
    """

    original: str
    cleaned: str
    was_injection_detected: bool
    matched_patterns: list[str] = field(default_factory=list)
    content_hash: str = ""


class PromptSanitizer:
    """Pré-filtro determinístico de prompt injection (sem chamada de LLM).

    Uso típico (defense-in-depth, antes de interpolar fonte no prompt do LLM)::

        sanitized = get_prompt_sanitizer().sanitize(result.description)
        if sanitized.was_injection_detected:
            logger.warning("prompt_injection_blocked", extra={...})  # só fingerprint
        prompt += sanitized.cleaned

    A instância é **stateless** e thread-safe (apenas regex compiladas em
    módulo). Use :func:`get_prompt_sanitizer` para obter o singleton.
    """

    def sanitize(self, content: str) -> SanitizationResult:
        """Remove padrões de prompt injection do conteúdo.

        Args:
            content: Texto de fonte externa a ser inspecionado/limpo.

        Returns:
            SanitizationResult: ``cleaned`` preserva o texto sem os trechos
            removidos; ``was_injection_detected`` indica se houve remoção.
            Em falha anômala do engine, retorna o conteúdo **original** (fail-open
            para não quebrar a geração do relatório) registrando o erro.
        """
        if not content:
            return SanitizationResult(
                original=content or "",
                cleaned=content or "",
                was_injection_detected=False,
                matched_patterns=[],
                content_hash=self._hash(content or ""),
            )

        content_hash = self._hash(content)
        try:
            cleaned = content
            matched: list[str] = []
            for name, pattern in _INJECTION_PATTERNS:
                if pattern.search(cleaned):
                    matched.append(name)
                    cleaned = pattern.sub(REMOVED_MARKER, cleaned)

            detected = bool(matched)
            if detected:
                # Loga APENAS fingerprint + nome do padrão — nunca o conteúdo.
                logger.warning(
                    "prompt_injection_blocked",
                    extra={
                        "fingerprint": content_hash,
                        "patterns": matched,
                        "chars": len(content),
                    },
                )
            return SanitizationResult(
                original=content,
                cleaned=cleaned,
                was_injection_detected=detected,
                matched_patterns=matched,
                content_hash=content_hash,
            )
        except Exception as e:  # pragma: no cover - defesa contra anomalia de engine
            logger.error(f"PromptSanitizer falhou inesperadamente: {e}")
            return SanitizationResult(
                original=content,
                cleaned=content,
                was_injection_detected=False,
                matched_patterns=[],
                content_hash=content_hash,
            )

    def sanitize_batch(self, contents: list[str]) -> list[SanitizationResult]:
        """Sanitiza múltiplos conteúdos (determinístico, sem paralelismo real)."""
        return [self.sanitize(c) for c in contents]

    @staticmethod
    def _hash(text: str) -> str:
        """SHA-256 do texto — usado como fingerprint em logs (não expõe conteúdo)."""
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


_singleton: PromptSanitizer | None = None


def get_prompt_sanitizer() -> PromptSanitizer:
    """Retorna o singleton :class:`PromptSanitizer` (stateless, thread-safe)."""
    global _singleton
    if _singleton is None:
        _singleton = PromptSanitizer()
    return _singleton
