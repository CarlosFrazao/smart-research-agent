"""Parser de operadores de busca avançada (Auditoria Parte 2 — Fase 6.5).

Permite que o usuário escreva operadores estilo Google diretamente na query:

    ``site:reddit.com melhor teclado mecânico``
    ``filetype:pdf machine learning``
    ``intitle:python tutorial``

O parser extrai os operadores para campos estruturados e devolve o texto limpo
(sem os operadores), para que cada searcher os aplique conforme suporta:
SearXNG e DuckDuckGo aceitam ``site:``/``filetype:`` nativamente; fontes de API
que não suportam recebem apenas o texto limpo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Operadores suportados. Cada um captura o token seguinte ao ``operador:``.
_SITE_RE = re.compile(r"\bsite:(\S+)", re.IGNORECASE)
_FILETYPE_RE = re.compile(r"\bfiletype:(\S+)", re.IGNORECASE)
_INTITLE_RE = re.compile(r"\bintitle:(\S+)", re.IGNORECASE)


@dataclass
class ParsedQuery:
    """Resultado da análise de operadores de busca avançada.

    Attributes:
        raw: Query original, sem modificações.
        text: Query sem os operadores (texto de busca "limpo").
        site_filter: Domínio de ``site:`` (ex: "reddit.com") ou ``None``.
        filetype: Extensão de ``filetype:`` (ex: "pdf") ou ``None``.
        intitle: Termo de ``intitle:`` (ex: "python") ou ``None``.
        extra_operators: Reservado para operadores futuros.
    """

    raw: str
    text: str
    site_filter: str | None = None
    filetype: str | None = None
    intitle: str | None = None
    extra_operators: dict[str, str] = field(default_factory=dict)

    @property
    def has_operators(self) -> bool:
        """True se ao menos um operador avançado foi detectado."""
        return bool(
            self.site_filter or self.filetype or self.intitle or self.extra_operators
        )

    def to_engine_query(self) -> str:
        """Reconstrói a query no formato nativo de engines tipo Google/SearXNG.

        Reanexa os operadores suportados nativamente (``site:``/``filetype:``/
        ``intitle:``) ao texto limpo. Usado por searchers que os aceitam.

        Returns:
            Query com operadores nativos reanexados (ex:
            ``"melhor teclado site:reddit.com"``).
        """
        parts = [self.text] if self.text else []
        if self.site_filter:
            parts.append(f"site:{self.site_filter}")
        if self.filetype:
            parts.append(f"filetype:{self.filetype}")
        if self.intitle:
            parts.append(f"intitle:{self.intitle}")
        return " ".join(parts).strip()


def parse_advanced_query(raw_query: str) -> ParsedQuery:
    """Parseia operadores de busca avançada de uma query de usuário.

    Exemplos:
        >>> parse_advanced_query("site:reddit.com best keyboard").site_filter
        'reddit.com'
        >>> parse_advanced_query("filetype:pdf machine learning").filetype
        'pdf'

    Args:
        raw_query: Query bruta digitada pelo usuário.

    Returns:
        :class:`ParsedQuery` com operadores extraídos e ``text`` limpo. Se a
        query for vazia/None, retorna um ParsedQuery vazio consistente.
    """
    raw = raw_query or ""
    query = raw
    result = ParsedQuery(raw=raw, text=raw)

    site_match = _SITE_RE.search(query)
    if site_match:
        result.site_filter = site_match.group(1)
        query = query.replace(site_match.group(0), "").strip()

    filetype_match = _FILETYPE_RE.search(query)
    if filetype_match:
        result.filetype = filetype_match.group(1)
        query = query.replace(filetype_match.group(0), "").strip()

    intitle_match = _INTITLE_RE.search(query)
    if intitle_match:
        result.intitle = intitle_match.group(1)
        query = query.replace(intitle_match.group(0), "").strip()

    # Normaliza espaços duplicados deixados pela remoção dos operadores.
    result.text = re.sub(r"\s+", " ", query).strip()
    return result


__all__ = ["ParsedQuery", "parse_advanced_query"]
