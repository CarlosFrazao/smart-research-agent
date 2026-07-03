"""
Taxonomia de exceções granulares do Smart Research Agent (SRA).
Dividido em erros transientes (podem ser retentados) e erros permanentes.
"""

class SRABaseError(Exception):
    """Erro base do SRA."""
    pass


class TransientError(SRABaseError):
    """Erro temporário — pode ser retentado (timeout, rate limit)."""
    def __init__(self, msg: str, retry_after: float = 30.0):
        super().__init__(msg)
        self.retry_after = retry_after


class PermanentError(SRABaseError):
    """Erro permanente — não retentar esta fonte/operação."""
    pass


class SearcherTimeoutError(TransientError):
    """Timeout ao buscar resultados."""
    pass


class SearcherRateLimitError(TransientError):
    """Rate limit atingido."""
    def __init__(self, msg: str, retry_after: float = 60.0):
        super().__init__(msg, retry_after=retry_after)


class SearcherBlockedError(PermanentError):
    """Bloqueado pelo provedor de pesquisa (403, 451, captcha)."""
    pass


class LLMError(TransientError):
    """Erro na chamada LLM ou no provedor (OpenRouter, Gemini, etc)."""
    pass


class BudgetExceededError(SRABaseError):
    """Budget de tokens, profundidade, nós ou custo esgotado."""
    pass
