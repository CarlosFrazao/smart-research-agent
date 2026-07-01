#!/usr/bin/env python3
"""
UserPromptSubmit hook — anti-query-vaga

Detecta queries de pesquisa ruins antes de serem executadas:
- Prompt < 15 chars (vago demais para gerar pesquisa útil)
- Palavra isolada sem contexto ("pesquisa", "busca", "isso")
- Error paste sem pergunta (stack trace colado sem instrução)
- Loop: mesmo fragmento enviado 2x seguidas

Latência: heurística pura, zero chamada LLM — < 30ms.
Saída: JSON para stdout conforme protocolo UserPromptSubmit do Claude Code.
  - {"decision": "block", "reason": "..."} bloqueia e mostra mensagem
  - {"decision": "approve"} deixa passar
"""

import sys
import json
import re
import os
import hashlib
from pathlib import Path


# ── Padrões ──────────────────────────────────────────────────────────────────

# Palavras vazias isoladas que não constroem uma query útil
_VAGUE_ONLY = re.compile(
    r"^(isso|aquilo|esse\s+troco|tipo\s+assim|ne|né|aí|pesquisa|busca|"
    r"search|find|olha|veja|mostra|faz|faça|tenta|ok|sim|não|nao)\.?$",
    re.IGNORECASE,
)

# Padrão de "tenta de novo" sem nova informação
_RETRY_PATTERN = re.compile(
    r"\b(tenta\s+(de\s+novo|outra\s+vez|novamente)|refaz|de\s+novo|"
    r"try\s+again|retry|tente\s+novamente)\b",
    re.IGNORECASE,
)

# Linha que parece stack trace / erro de código
_ERROR_LINE = re.compile(
    r"\b(at\s+\w+|Error:|Exception:|Traceback|stack:|TypeError|"
    r"ReferenceError|SyntaxError|AttributeError|ValueError|ImportError|"
    r"ModuleNotFoundError|KeyError)\b"
)

# Comandos slash — sempre deixa passar
_SLASH_CMD = re.compile(r"^/")

# Perguntas explícitas — deixa passar
_QUESTION_STARTERS = re.compile(
    r"^(como|qual|quando|onde|por\s*que|porqu[eê]|quem|o\s*que|"
    r"what|how|why|where|when|which|who)\b",
    re.IGNORECASE,
)

# Whitelist de respostas curtas legítimas em diálogo
_WHITELIST = re.compile(
    r"^(ok|sim|nao|não|prossiga|continue|skip|cancela|cancelar|"
    r"abort|stop|para|pare|yes|no|next|confirm|confirma|commit|"
    r"commit\s+this|git\s+commit|push|git\s+push)\.?$",
    re.IGNORECASE,
)

# ── Cache de histórico leve ───────────────────────────────────────────────────

_HISTORY_FILE = Path(os.environ.get("SMART_RESEARCH_HOOK_CACHE", "/tmp")) / "sra_prompt_history.jsonl"
_MAX_HISTORY = 10


def _load_history() -> list[str]:
    try:
        lines = _HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        return [json.loads(l).get("prompt", "") for l in lines[-_MAX_HISTORY:] if l.strip()]
    except Exception:
        return []


def _append_history(prompt: str) -> None:
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"prompt": prompt}) + "\n")
        # Rotaciona se passar de MAX_HISTORY * 3 linhas
        lines = _HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_HISTORY * 3:
            _HISTORY_FILE.write_text("\n".join(lines[-_MAX_HISTORY:]) + "\n", encoding="utf-8")
    except Exception:
        pass


# ── Analisadores ─────────────────────────────────────────────────────────────

def _is_error_paste(prompt: str) -> bool:
    lines = [l for l in prompt.splitlines() if l.strip()]
    if len(lines) < 3:
        return False
    error_count = sum(1 for l in lines if _ERROR_LINE.search(l))
    return error_count >= 1 and "?" not in prompt


def _is_loop(prompt: str, history: list[str]) -> bool:
    trimmed = prompt.strip().lower()
    recent = [h.strip().lower() for h in history[-3:]]
    return trimmed in recent


def analyze(prompt: str, history: list[str]) -> tuple[bool, list[str]]:
    """Retorna (flagged, reasons). Puro — sem I/O."""
    reasons: list[str] = []
    trimmed = prompt.strip()

    # Whitelist imediata
    if _SLASH_CMD.match(trimmed):
        return False, []
    if _WHITELIST.match(trimmed):
        return False, []
    if _QUESTION_STARTERS.match(trimmed):
        return False, []
    if "?" in trimmed:
        return False, []

    # Muito curto e não é pergunta clara
    if len(trimmed) < 15 and not _QUESTION_STARTERS.match(trimmed):
        reasons.append(
            f"query muito curta ({len(trimmed)} chars) — descreva o que quer pesquisar "
            "com mais contexto (ex: 'CRM open source parecido com HubSpot')"
        )

    # Palavra vaga isolada
    if _VAGUE_ONLY.match(trimmed):
        reasons.append(
            "query vaga sem referente — especifique o tema, tecnologia ou produto"
        )

    # Erro colado sem instrução
    if _is_error_paste(trimmed):
        reasons.append(
            "parece um stack trace sem instrução — diga o que quer fazer com esse erro"
        )

    # Loop — mesmo prompt repetido
    if _is_loop(trimmed, history):
        reasons.append(
            "query idêntica a uma enviada recentemente — "
            "reformule com mais contexto ou use /research_technology_v2 com mode=deep"
        )

    # "Tenta de novo" sem contexto
    if _RETRY_PATTERN.search(trimmed) and len(trimmed) < 80:
        reasons.append(
            "detectei 'tenta de novo' sem novo contexto — "
            "explique o que mudou ou qual resultado esperava"
        )

    return len(reasons) > 0, reasons


# ── Entrada principal ─────────────────────────────────────────────────────────

def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    prompt: str = data.get("prompt", "")

    if not prompt:
        print(json.dumps({"decision": "approve"}))
        return

    history = _load_history()
    flagged, reasons = analyze(prompt, history)
    _append_history(prompt)

    if not flagged:
        print(json.dumps({"decision": "approve"}))
        return

    suggestion = (
        "💡 Dica: queries boas têm 20-100 chars e especificam tema + contexto.\n"
        "Exemplos:\n"
        "  ✅ 'melhores ferramentas self-hosted para RAG com LLM em 2026'\n"
        "  ✅ 'alternativas open source ao Zapier para automação'\n"
        "  ❌ 'pesquisa'  ❌ 'busca isso'  ❌ 'tenta de novo'"
    )

    reason_text = "\n".join(f"• {r}" for r in reasons)
    message = (
        f"🔍 Anti-Query-Vaga detectou problemas:\n\n"
        f"{reason_text}\n\n"
        f"{suggestion}"
    )

    print(json.dumps({"decision": "block", "reason": message}))


if __name__ == "__main__":
    main()
