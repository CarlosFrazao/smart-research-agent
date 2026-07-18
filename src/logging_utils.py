"""
logging_utils — Utilitários de logging tolerantes a Windows para o SRA.

Este módulo consolida dois padrões portados do Hermes Agent (Nous Research,
MIT) que resolvem dores reais do SRA em ambiente Windows:

1. ``RotatingFileHandler`` tolerante a multi-processo
   No Windows, o ``RotatingFileHandler`` da stdlib chama ``os.rename()`` no
   ``doRollover()`` e falha com ``PermissionError [WinError 32]`` quando outro
   processo mantém um handle aberto no mesmo arquivo de log.  O SRA já sofreu
   com problemas de lock/concorrência em Windows (KuzuDB), então usamos
   ``concurrent_log_handler.ConcurrentRotatingFileHandler`` no Windows — ele
   serializa o rename com um lock cross-process (via ``portalocker``/pywin32)
   para que apenas um processo rotacione por vez.  No POSIX usamos a stdlib,
   que já renomeia arquivos abertos sem problema.

2. ``RedactingFormatter``
   Um ``logging.Formatter`` que mascara segredos (API keys, tokens, senhas,  # pragma: allowlist secret
   conection strings, JWTs) antes de qualquer linha ser escrita em disco.
   Alinhado ao Bloco E7-T1 (detect-secrets / audit log) do SRA: o log de  # pragma: allowlist secret
   auditoria nunca deve persistir credenciais em texto claro.

O módulo é self-contained: não importa nada do Hermes, apenas stdlib +
``concurrent_log_handler`` (opcional, apenas no Windows).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from logging import Formatter
from logging.handlers import TimedRotatingFileHandler as _StdlibTimedRotatingFileHandler
from typing import Optional

# ---------------------------------------------------------------------------
# 1. Handlers de rotação tolerantes a Windows
# ---------------------------------------------------------------------------
# No Windows trocamos as stdlib por variantes do ``concurrent_log_handler``,
# que envolvem o rename num lock cross-process (via ``portalocker``/pywin32)
# para evitar ``PermissionError [WinError 32]`` quando vários processos
# escrevem no mesmo arquivo.  Fora do Windows mantemos a stdlib (funciona
# corretamente em POSIX).  Os aliases preservam as referências existentes sem
# mudança de código nos chamadores.
if sys.platform == "win32":
    try:
        from concurrent_log_handler import (  # type: ignore[import-not-found]
            ConcurrentRotatingFileHandler as RotatingFileHandler,
        )
    except ImportError:  # pragma: no cover - fallback defensivo
        from logging.handlers import RotatingFileHandler  # type: ignore[no-redef]

    try:
        from concurrent_log_handler import (  # type: ignore[import-not-found]
            ConcurrentTimedRotatingFileHandler as TimedRotatingFileHandler,
        )
    except ImportError:  # pragma: no cover - fallback defensivo
        from logging.handlers import TimedRotatingFileHandler  # type: ignore[no-redef]
else:
    from logging.handlers import RotatingFileHandler  # type: ignore[no-redef]
    from logging.handlers import TimedRotatingFileHandler  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# 2. Redaction de segredos
# ---------------------------------------------------------------------------

# Ativado por padrão; pode ser desligado via SRA_REDACT_SECRETS=false (snapshot  # pragma: allowlist secret
# em import-time para que mutações de env em runtime não desativem a redaction).
_REDACT_ENABLED = os.getenv(
    "SRA_REDACT_SECRETS", "true"
).lower() in {  # pragma: allowlist secret
    "1",
    "true",
    "yes",
    "on",
}

# Prefixos conhecidos de API keys/tokens (subconjunto relevante para o SRA).  # pragma: allowlist secret
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",  # OpenAI / OpenRouter / Anthropic (sk-ant-*)  # pragma: allowlist secret
    r"ghp_[A-Za-z0-9]{10,}",  # GitHub PAT (classic)  # pragma: allowlist secret
    r"github_pat_[A-Za-z0-9_]{10,}",  # GitHub PAT (fine-grained)
    r"AIza[A-Za-z0-9_-]{30,}",  # Google API keys
    r"pplx-[A-Za-z0-9]{10,}",  # Perplexity
    r"fc-[A-Za-z0-9]{10,}",  # Firecrawl
    r"AKIA[A-Z0-9]{16}",  # AWS Access Key ID  # pragma: allowlist secret
    r"sk_live_[A-Za-z0-9]{10,}",  # Stripe secret key (live)  # pragma: allowlist secret
    r"SG\.[A-Za-z0-9_-]{10,}",  # SendGrid API key
    r"hf_[A-Za-z0-9]{10,}",  # HuggingFace token  # pragma: allowlist secret
    r"r8_[A-Za-z0-9]{10,}",  # Replicate API token  # pragma: allowlist secret
    r"npm_[A-Za-z0-9]{10,}",  # npm access token  # pragma: allowlist secret
    r"pypi-[A-Za-z0-9_-]{10,}",  # PyPI API token  # pragma: allowlist secret
    r"gsk_[A-Za-z0-9]{10,}",  # Groq Cloud API key
    r"tvly-[A-Za-z0-9]{10,}",  # Tavily search API key
    r"exa_[A-Za-z0-9]{10,}",  # Exa search API key
    r"xai-[A-Za-z0-9]{30,}",  # xAI (Grok) API key
    r"ntn_[A-Za-z0-9]{10,}",  # Notion internal integration token  # pragma: allowlist secret
    r"fw-[A-Za-z0-9]{30,}",  # Fireworks AI API key
    r"fw_[A-Za-z0-9]{30,}",  # Fireworks AI API key
    r"sk-[A-Za-z0-9]{10,}",  # Anthropic / generic sk- dash form
]

# Cabeçalhos de autorização (qualquer esquema) — preserva nome+scheme, mascara o token.  # pragma: allowlist secret
_AUTH_HEADER_RE = re.compile(
    r"((?:Proxy-)?Authorization:\s*)([A-Za-z][\w.+-]*\s+)?([^\s\"']+)",  # pragma: allowlist secret
    re.IGNORECASE,
)

# Cabeçalhos de API key (x-api-key etc.) — valor único opaco.  # pragma: allowlist secret
_SECRET_HEADER_RE = re.compile(  # pragma: allowlist secret
    r"((?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|x-auth-token|x-access-token)\s*:\s*)(\S+)",  # pragma: allowlist secret
    re.IGNORECASE,
)

# Connection strings de banco: scheme://user:SENHA@host  # pragma: allowlist secret
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s]+:)([^@\s]+)(@)",
    re.IGNORECASE,
)

# Blocos de chave privada PEM.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"  # pragma: allowlist secret
)

# JWTs: header.payload[.signature] (sempre começam com "eyJ").  # pragma: allowlist secret
_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}"
)  # pragma: allowlist secret

# Tokens em userinfo de URL: scheme://TOKEN@host (sem user:pass).  # pragma: allowlist secret
_URL_BARE_TOKEN_RE = re.compile(  # pragma: allowlist secret
    r"((?:https?|wss?|git|ssh|ftp|ftps|sftp)://)" r"([^\s:@/]{8,})" r"(@[^\s]+)",
    re.IGNORECASE,
)

# Atribuições de env: NOME_SECRETO=value (chave em uppercase).  # pragma: allowlist secret
_ENV_ASSIGN_RE = re.compile(
    r"([A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Z0-9_]*)\s*=\s*(['\"]?)(\S+)\2"  # pragma: allowlist secret
)

# Campos JSON: "apiKey": "value", etc.  # pragma: allowlist secret
_JSON_KEY_NAMES = (
    r"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|"  # pragma: allowlist secret
    r"auth_token|bearer|secret_value|raw_secret|secret_input|key_material)"  # pragma: allowlist secret
)
_JSON_FIELD_RE = re.compile(
    rf'("{_JSON_KEY_NAMES}")\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)

_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)


def mask_secret(  # pragma: allowlist secret
    value: str,
    *,
    head: int = 4,
    tail: int = 4,
    floor: int = 12,
    placeholder: str = "***",
    empty: str = "",
) -> str:
    """Mascara um segredo preservando ``head`` e ``tail`` caracteres.

    Valores menores que ``head + tail + floor_margin`` são totalmente mascarados.

    Args:
        value: Segredo a mascarar. ``None``/vazio retorna ``empty``.
        head: Caracteres iniciais preservados.
        tail: Caracteres finais preservados.
        floor: Abaixo disto o valor é totalmente mascarado.
        placeholder: Retornado para valores curtos demais.
        empty: Retornado quando ``value`` é falsy.

    Returns:
        Segredo mascarado (ou ``empty``/``placeholder``).
    """
    if not value:
        return empty
    if len(value) < floor:
        return placeholder
    return f"{value[:head]}...{value[-tail:]}"


def _mask_token(token: str) -> str:  # pragma: allowlist secret
    """Mascara um token de log — floor conservador de 18 chars, preserva 6/4."""  # pragma: allowlist secret
    if not token:  # pragma: allowlist secret
        return "***"
    return mask_secret(token, head=6, tail=4, floor=18)  # pragma: allowlist secret


def redact_sensitive_text(text: str, *, force: bool = False) -> str:
    """Aplica padrões de redaction a um bloco de texto.

    Seguro para qualquer string — texto sem correspondência passa inalterado.
    Ativado por padrão; desligável via ``SRA_REDACT_SECRETS=false``.  Use  # pragma: allowlist secret
    ``force=True`` em fronteiras de segurança que nunca devem retornar segredos
    crus independente da preferência global.

    Args:
        text: Texto a redactar.
        force: Se True, redacta independente do flag global.

    Returns:
        Texto com segredos mascarados.
    """
    if text is None:
        return None  # type: ignore[return-value]
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    if not (force or _REDACT_ENABLED):
        return text

    # Prefixos conhecidos (sk-, ghp_, etc.) — gate em substring.  # pragma: allowlist secret
    if _has_known_prefix_substring(text):
        text = _PREFIX_RE.sub(
            lambda m: _mask_token(m.group(1)), text
        )  # pragma: allowlist secret

    # Cabeçalhos Authorization (qualquer scheme).  # pragma: allowlist secret
    if "authorization" in text.lower():  # pragma: allowlist secret
        text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2) or ''}***", text)

    # Cabeçalhos de API key (x-api-key etc.).  # pragma: allowlist secret
    if ":" in text:
        text = _SECRET_HEADER_RE.sub(
            lambda m: f"{m.group(1)}***", text
        )  # pragma: allowlist secret

    # Connection strings de banco.
    if "://" in text:
        text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)
        # Tokens em userinfo de URL (scheme://TOKEN@host).  # pragma: allowlist secret
        text = _URL_BARE_TOKEN_RE.sub(
            lambda m: f"{m.group(1)}***{m.group(3)}", text
        )  # pragma: allowlist secret

    # Blocos de chave privada.
    if "PRIVATE KEY" in text:  # pragma: allowlist secret
        text = _PRIVATE_KEY_RE.sub(
            "-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----",  # pragma: allowlist secret
            text,  # pragma: allowlist secret
        )

    # JWTs.
    if "eyJ" in text:  # pragma: allowlist secret
        text = _JWT_RE.sub("***", text)

    # Atribuições de env (OPENAI_API_KEY=...).
    if "=" in text:
        text = _ENV_ASSIGN_RE.sub(
            lambda m: (
                f"{m.group(1)}={m.group(2)}{_mask_token(m.group(3))}{m.group(2)}"
            ),  # pragma: allowlist secret
            text,
        )

    # Campos JSON ("apiKey": "value").  # pragma: allowlist secret
    if '"' in text:
        text = _JSON_FIELD_RE.sub(lambda m: f'{m.group(1)}:"***"', text)

    return text


# Substrings de pre-check: se nenhuma aparece no texto, os prefixos não casam.
_PREFIX_PRECHECK = (
    "sk-",
    "ghp_",  # pragma: allowlist secret
    "github_pat_",
    "AIza",
    "pplx-",
    "fc-",
    "AKIA",  # pragma: allowlist secret
    "sk_live_",
    "SG.",
    "hf_",
    "r8_",
    "npm_",
    "pypi-",  # pragma: allowlist secret
    "gsk_",
    "tvly-",
    "exa_",
    "xai-",
    "ntn_",
    "fw-",
    "fw_",
)


def _has_known_prefix_substring(text: str) -> bool:
    """Pre-check barato: True se algum prefixo conhecido aparece no texto."""
    return any(sub in text for sub in _PREFIX_PRECHECK)


class RedactingFormatter(Formatter):
    """``logging.Formatter`` que mascara segredos de toda linha de log.

    Usage::

        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    """

    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
        style: str = "%",
        **kwargs: object,
    ) -> None:
        super().__init__(fmt, datefmt, style, **kwargs)

    def format(self, record: logging.LogRecord) -> str:
        """Formata o record e redacta segredos do resultado."""
        original = super().format(record)
        return redact_sensitive_text(original)


# ---------------------------------------------------------------------------
# 3. Deduplicação de handlers + configuração central do root logger (F6/D1)
# ---------------------------------------------------------------------------
# Sintoma observado (F6): múltiplos ``basicConfig``/``addHandler`` espalhados
# (main.py, mcp_server.py, proxy_manager.py, utils/logging.py) adicionam handlers
# repetidos ao root logger → cada linha de log aparece 2x+ (stdout duplicado ou
# stdout+file iguais).  Estas funções garantem no máximo 1 StreamHandler (stdout)
# + 1 FileHandler por logger, eliminando a duplicação de forma idempotente.


def _handler_signature(handler: logging.Handler) -> Optional[tuple]:
    """Retorna uma assinatura estável para detectar handlers duplicados.

    Agrupa por: (tipo, stream_destino_ou_caminho_arquivo).  Dois handlers com a
    mesma assinatura em um mesmo logger são considerados duplicados — apenas o
    primeiro é mantido.
    """
    if isinstance(handler, logging.FileHandler):
        path = getattr(handler, "baseFilename", None)
        if path is None and isinstance(handler, RotatingFileHandler):
            path = getattr(handler, "baseFilename", None)
        return ("file", path)
    if isinstance(handler, logging.StreamHandler) and not isinstance(
        handler, logging.FileHandler
    ):
        stream = getattr(handler, "stream", None)
        dest = (
            "stdout"
            if stream is sys.stdout
            else ("stderr" if stream is sys.stderr else id(stream))
        )
        return ("stream", dest)
    # TimedRotating/Rotating stdlib também são FileHandler (cobertos acima);
    # qualquer outro tipo usa a classe como chave.
    return ("other", type(handler).__qualname__)


def dedupe_handlers(logger: logging.Logger) -> int:
    """Remove handlers duplicados de ``logger`` (mantém o primeiro de cada assinatura).

    Idempotente: chamar repetidas vezes não altera um logger já desduplicado.
    Retorna o número de handlers removidos.
    """
    seen: set = set()
    kept: list[logging.Handler] = []
    removed = 0
    for handler in logger.handlers:
        sig = _handler_signature(handler)
        if sig in seen:
            removed += 1
            continue
        seen.add(sig)
        kept.append(handler)
    if removed:
        logger.handlers[:] = kept
    return removed


def dedupe_root_handlers() -> int:
    """Desduplica os handlers do root logger (caso mais comum de F6)."""
    return dedupe_handlers(logging.getLogger())


# Formato padrão usado por ``configure_root_logger`` — redação aplicada via
# RedactingFormatter para nunca persistir segredos em disco.
_DEFAULT_FMT = (
    "%(asctime)s [%(levelname)s] %(name)s [corr=%(correlation_id)s]: %(message)s"
)
_DEFAULT_DATEFMT = "%H:%M:%S"


def configure_root_logger(
    level: str = "INFO",
    *,
    log_file: Optional[str] = None,
    fmt: Optional[str] = None,
    datefmt: Optional[str] = None,
    force: bool = False,
) -> logging.Logger:
    """Configura o root logger com no máximo 1 stdout + 1 file handler (F6/D1).

    - Sempre desduplica antes de adicionar (idempotente).
    - Sem ``log_file``: garante exatamente 1 ``StreamHandler(stdout)``.
    - Com ``log_file``: garante 1 stdout + 1 ``FileHandler`` (redação em disco).
    - Respeita ``force`` para limpar handlers preexistentes quando desejado.

    Args:
        level: Nível de log ("DEBUG"/"INFO"/...).
        log_file: Caminho opcional de arquivo de log.
        fmt: Formato de linha (default SRA).
        datefmt: Formato de data.
        force: Se True, remove todos os handlers antes de (re)configurar.

    Returns:
        O root logger configurado.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    _fmt = fmt or _DEFAULT_FMT
    _datefmt = datefmt or _DEFAULT_DATEFMT

    # 1) stdout — garante exatamente 1 StreamHandler(stdout).
    has_stdout = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and getattr(h, "stream", None) is sys.stdout
        for h in root.handlers
    )
    if not has_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(RedactingFormatter(_fmt, datefmt=_datefmt))
        sh.addFilter(CorrelationIdFilter())
        root.addHandler(sh)

    # 2) file — garante no máximo 1 FileHandler apontando para log_file.
    if log_file:
        file_path = os.path.abspath(log_file)
        has_file = any(
            isinstance(h, logging.FileHandler)
            and getattr(h, "baseFilename", None) == file_path
            for h in root.handlers
        )
        if not has_file:
            fh = logging.FileHandler(file_path, encoding="utf-8")
            fh.setFormatter(RedactingFormatter(_fmt, datefmt=_datefmt))
            fh.addFilter(CorrelationIdFilter())
            root.addHandler(fh)

    # Remove qualquer duplicata remanescente (idempotente).
    dedupe_handlers(root)
    return root


class CorrelationIdFilter(logging.Filter):
    """Filtro que injeta ``correlation_id`` (default ``-``) em todo ``LogRecord``.

    Mantido aqui para que ``configure_root_logger`` seja self-contained e não
    dependa de ``src.utils.logging`` (evita import circular / overhead).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = "-"
        return True
