"""Módulo de Personas de Domínio e Formatação de Citações do SRA.

Implementa regras de formatação de referências baseadas em normas acadêmicas
e legais (APA, IEEE, Bluebook) adaptadas aos tipos de resultados do SRA.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional
from src.types import SearchResult, Domain

logger = logging.getLogger("domain-personas")


class DomainPersona:
    """Orquestra a formatação de citações e referências baseadas no domínio de pesquisa."""

    def __init__(self, domain: Domain) -> None:
        self.domain = domain
        self._formatter = self._get_formatter_for_domain(domain)

    def _get_formatter_for_domain(self, domain: Domain):
        """Mapeia o domínio de pesquisa para a persona/norma de citação correspondente."""
        # Domínios de infra/tecnologia/acadêmico -> IEEE (Técnico)
        if domain in (
            Domain.DEV_TOOLS,
            Domain.AI_ML,
            Domain.INFRASTRUCTURE,
            Domain.OPEN_SOURCE,
        ):
            return self.format_ieee
        # Domínios de negócios/SaaS/Geral -> APA (Geral)
        elif domain in (Domain.SAAS_B2B, Domain.AUTOMATION, Domain.GENERAL):
            return self.format_apa
        # Fallback padrão
        return self.format_apa

    def format_citation(self, result: SearchResult, index: Optional[int] = None) -> str:
        """Formata um SearchResult em uma citação textual legível."""
        return self._formatter(result, index)

    def _extract_metadata(self, r: SearchResult) -> Dict[str, Any]:
        """Extrai metadados de forma defensiva a partir dos payloads flexíveis do SearchResult."""
        # 1. Autor
        author = r.metrics.get("author") or r.raw.get("author") or r.raw.get("by")
        if not author and r.source == "github":
            author = r.raw.get("owner", {}).get("login") or r.raw.get("owner")

        # 2. Fonte / Site
        source = r.source.upper() if r.source else "WEB"

        # 3. Data / Ano
        date_raw = (
            r.raw.get("published_date")
            or r.raw.get("created_at")
            or r.raw.get("time")
            or r.raw.get("date")
        )
        year = None
        date_str = None

        if date_raw:
            if isinstance(date_raw, (int, float)):
                try:
                    dt = datetime.fromtimestamp(date_raw)
                    year = str(dt.year)
                    date_str = dt.strftime("%Y, %B %d")
                except Exception:
                    pass
            elif isinstance(date_raw, str):
                # Tenta extrair o ano (primeiros 4 dígitos sequenciais)
                import re

                match = re.search(r"\b(19|20)\d{2}\b", date_raw)
                if match:
                    year = match.group(0)
                date_str = date_raw

        if not year and isinstance(r.fetched_at, datetime):
            year = str(r.fetched_at.year)
            date_str = r.fetched_at.strftime("%Y, %B %d")
        elif not year:
            year = "n.d."
            date_str = "n.d."

        return {
            "author": author,
            "title": r.title or "Sem título",
            "source": source,
            "url": r.url or "",
            "year": year,
            "date_str": date_str,
        }

    def format_apa(self, r: SearchResult, index: Optional[int] = None) -> str:
        """Formata a citação seguindo o padrão APA (American Psychological Association).

        Formato: Autor, A. A. (Ano). Título. Fonte. URL
        """
        meta = self._extract_metadata(r)
        author_part = meta["author"] if meta["author"] else meta["title"]
        title_part = f" *{meta['title']}*" if meta["author"] else ""

        ref = f"{author_part}. ({meta['year']}).{title_part} {meta['source']}."
        if meta["url"]:
            ref += f" Disponível em: {meta['url']}"

        idx_prefix = f"[{index}] " if index is not None else ""
        return f"{idx_prefix}{ref}"

    def format_ieee(self, r: SearchResult, index: Optional[int] = None) -> str:
        """Formata a citação seguindo o padrão IEEE (normas técnicas/engenharia).

        Formato: [No.] Autor, "Título," Fonte, Ano. [Online]. Available: URL
        """
        meta = self._extract_metadata(r)
        idx = index if index is not None else 1

        author_part = f"{meta['author']}" if meta["author"] else "anon."

        ref = f'{author_part}, "{meta["title"]}", {meta["source"]}, {meta["year"]}.'
        if meta["url"]:
            ref += f" [Online]. Available: {meta['url']}"

        return f"[{idx}] {ref}"

    def format_bluebook(self, r: SearchResult, index: Optional[int] = None) -> str:
        """Formata a citação seguindo o padrão Bluebook (Legal Citation).

        Formato: Autor, Título, FONTE (Data), URL
        """
        meta = self._extract_metadata(r)
        author_part = f"{meta['author']}, " if meta["author"] else ""

        ref = f"{author_part}{meta['title']}, {meta['source']} ({meta['date_str']})"
        if meta["url"]:
            ref += f", {meta['url']}"

        idx_prefix = f"[{index}] " if index is not None else ""
        return f"{idx_prefix}{ref}"
