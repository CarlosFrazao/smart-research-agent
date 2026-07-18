"""X/Twitter searcher backed by xAI's built-in ``x_search`` Responses API tool.

Adapted from the Hermes Agent ``tools/x_search_tool.py`` (MIT, read-only source).
Reuses the pure helper logic (date validation, handle normalization, citation
extraction, degraded detection) but drops every Hermes-specific dependency
(``tools.registry``, ``tools.xai_http``, SuperGrok OAuth, ``requests``) and
plugs into the SRA ``BaseSearcher`` contract.

Security invariant:
    The xAI bearer token (``XAI_API_KEY``) is NEVER logged. Only ``source``,
    ``model`` and ``degraded_reason`` are allowed in log lines. Queries are
    passed through ``redact_sensitive_text`` before logging.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

from src.logging_utils import redact_sensitive_text
from src.search.base_searcher import BaseSearcher
from src.search.registry import register_searcher
from src.types import SearchResult

logger = logging.getLogger(__name__)

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_X_SEARCH_MODEL = "grok-4.20-reasoning"
MAX_HANDLES = 10


# ---------------------------------------------------------------------------
# Pure helpers (ported from Hermes tools/x_search_tool.py)
# ---------------------------------------------------------------------------


def _normalize_handles(handles: Any, field_name: str) -> list[str]:
    """Normalize a list of X handles, stripping leading ``@`` and whitespace.

    Raises ``ValueError`` when more than ``MAX_HANDLES`` entries remain.
    """
    cleaned: list[str] = []
    for handle in handles or []:
        normalized = str(handle or "").strip().lstrip("@")
        if normalized:
            cleaned.append(normalized)
    if len(cleaned) > MAX_HANDLES:
        raise ValueError(f"{field_name} supports at most {MAX_HANDLES} handles")
    return cleaned


def _parse_iso_date(value: str, field_name: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` string into a ``date``.

    xAI accepts arbitrary strings in ``from_date``/``to_date`` and silently
    returns an answer with no citations when the value is malformed. Validating
    client-side fails fast instead of burning a billable call.
    """
    raw = value.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD (got {raw!r})") from exc


def _validate_date_range(from_date: str, to_date: str) -> None:
    """Validate ``from_date`` / ``to_date`` before they reach xAI.

    Rules:
      * Either non-empty field must parse as ``YYYY-MM-DD``.
      * When both are set, ``from_date <= to_date``.
      * ``from_date`` must not be later than today UTC (X only indexes past
        posts). ``to_date`` in the future is allowed.
    """
    parsed_from: date | None = None
    parsed_to: date | None = None
    if from_date.strip():
        parsed_from = _parse_iso_date(from_date, "from_date")
    if to_date.strip():
        parsed_to = _parse_iso_date(to_date, "to_date")
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise ValueError(
            f"from_date ({parsed_from.isoformat()}) must be on or before "
            f"to_date ({parsed_to.isoformat()})"
        )
    if parsed_from is not None:
        today_utc = datetime.now(timezone.utc).date()
        if parsed_from > today_utc:
            raise ValueError(
                f"from_date ({parsed_from.isoformat()}) is in the future; "
                f"X Search only indexes past posts (today UTC is "
                f"{today_utc.isoformat()})"
            )


def _extract_response_text(payload: dict[str, Any]) -> str:
    """Extract the assistant answer text from an xAI Responses payload."""
    output_text = str(payload.get("output_text") or "").strip()
    if output_text:
        return output_text

    parts: list[str] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            ctype = content.get("type")
            if ctype in {"output_text", "text"}:
                text = str(content.get("text") or "").strip()
                if text:
                    parts.append(text)
    return "\n\n".join(parts).strip()


def _extract_inline_citations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract inline ``url_citation`` annotations from an xAI response."""
    citations: list[dict[str, Any]] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            for annotation in content.get("annotations", []) or []:
                if annotation.get("type") != "url_citation":
                    continue
                citations.append(
                    {
                        "url": annotation.get("url", ""),
                        "title": annotation.get("title", ""),
                        "start_index": annotation.get("start_index"),
                        "end_index": annotation.get("end_index"),
                    }
                )
    return citations


# ---------------------------------------------------------------------------
# Searcher
# ---------------------------------------------------------------------------


@register_searcher(
    "x", requires_key="XAI_API_KEY", enabled_env="SRA_X_ENABLED", trusted=True
)
class XSearcher(BaseSearcher):
    """Search X/Twitter via xAI's ``x_search`` Responses API tool.

    Maps the Hermes degraded signal onto SRA's ``evidence_quality`` /
    ``hallucination_flags`` so downstream stages can tell a real,
    citation-backed answer from one synthesized by the model.
    """

    def __init__(self, config: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """Initialize the searcher, applying the X-specific timeout override.

        Args:
            config: SRA configuration dict.
            **kwargs: Fallback parameters when ``config`` is not a dict.
        """
        super().__init__(config, **kwargs)
        # X search can take minutes; allow a longer per-request timeout.
        self.timeout = config.get("x_timeout", 180) if config else 180
        self.model = (
            config.get("x_model", DEFAULT_X_SEARCH_MODEL)
            if config
            else DEFAULT_X_SEARCH_MODEL
        )
        self.api_key = os.getenv("XAI_API_KEY")
        self.base_url = (os.getenv("XAI_BASE_URL") or DEFAULT_XAI_BASE_URL).rstrip("/")

    async def search(
        self,
        query: str,
        from_date: str = "",
        to_date: str = "",
        allowed_handles: list[str] | None = None,
        excluded_handles: list[str] | None = None,
        **kwargs: Any,
    ) -> list[SearchResult]:
        """Run an X search through the xAI Responses API.

        Args:
            query: The natural-language question to pose to X search.
            from_date: Optional ``YYYY-MM-DD`` lower bound (past only).
            to_date: Optional ``YYYY-MM-DD`` upper bound.
            allowed_handles: Restrict to these handles (max 10).
            excluded_handles: Exclude these handles (max 10).
            **kwargs: Ignored extras for interface compatibility.

        Returns:
            list[SearchResult]: A single normalized result, or an empty list
            (fallback) on validation failure or upstream error.
        """
        if not query or not query.strip():
            logger.warning("x_search: query vazia — fallback")
            return self.fallback(query)

        # Fail fast on invalid / mutually-exclusive inputs. Never calls the API.
        try:
            allowed = _normalize_handles(allowed_handles, "allowed_handles")
            excluded = _normalize_handles(excluded_handles, "excluded_handles")
            if allowed and excluded:
                logger.warning(
                    "x_search: allowed_handles e excluded_handles sao mutuamente exclusivos"
                )
                return self.fallback(query)
            _validate_date_range(from_date, to_date)
        except ValueError as exc:
            logger.warning("x_search: validacao falhou (%s) — fallback", exc)
            return self.fallback(query)

        # Build the x_search tool payload from the active narrowing filters.
        tool_def: dict[str, Any] = {"type": "x_search"}
        if allowed:
            tool_def["allowed_x_handles"] = allowed
        if excluded:
            tool_def["excluded_x_handles"] = excluded
        if from_date.strip():
            tool_def["from_date"] = from_date.strip()
        if to_date.strip():
            tool_def["to_date"] = to_date.strip()

        payload = {
            "model": self.model,
            "input": [{"role": "user", "content": query.strip()}],
            "tools": [tool_def],
            "store": False,
        }

        headers = {
            # SECURITY: the bearer value is interpolated here for the wire call
            # ONLY. It must never be passed to any logger call below.
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        logger.info(
            "x_search: chamando source=x model=%s query=%s",
            self.model,
            redact_sensitive_text(query.strip()),
        )

        try:
            response = await self._http_request(
                "POST",
                f"{self.base_url}/responses",
                headers=headers,
                json_body=payload,
            )
            data = response.json()
        except Exception as exc:
            logger.error("x_search: falha na chamada upstream — %s", type(exc).__name__)
            return self.fallback(query)

        answer = _extract_response_text(data)
        citations: list[dict[str, Any]] = list(data.get("citations") or [])
        inline_citations = _extract_inline_citations(data)

        active_filters: list[str] = []
        if allowed:
            active_filters.append("allowed_x_handles")
        if excluded:
            active_filters.append("excluded_x_handles")
        if from_date.strip():
            active_filters.append("from_date")
        if to_date.strip():
            active_filters.append("to_date")
        degraded = bool(active_filters) and not citations and not inline_citations
        degraded_reason = (
            f"sem citacoes apesar dos filtros: {', '.join(active_filters)}"
            if degraded
            else None
        )

        if degraded:
            logger.info(
                "x_search: resposta degraded (modelo, nao indice X) — %s",
                degraded_reason,
            )

        result = self.normalize(
            {
                "query": query.strip(),
                "answer": answer,
                "citations": citations,
                "inline_citations": inline_citations,
                "degraded": degraded,
                "degraded_reason": degraded_reason,
            }
        )
        if result is None:
            return self.fallback(query)
        return [result]

    def normalize(self, raw: dict[str, Any]) -> SearchResult | None:
        """Normalize a raw X search payload into a ``SearchResult``.

        Args:
            raw: Dict with keys ``query``, ``answer``, ``citations``,
                ``inline_citations``, ``degraded``, ``degraded_reason``.

        Returns:
            SearchResult | None: Normalized result, or ``None`` if the answer
            is empty (nothing usable to surface).
        """
        answer = str(raw.get("answer") or "").strip()
        if not answer:
            return None

        citations = raw.get("citations") or []
        citation_urls = [str(c.get("url", "")) for c in citations if c.get("url")]
        inline = raw.get("inline_citations") or []
        inline_urls = [str(c.get("url", "")) for c in inline if c.get("url")]
        all_urls = citation_urls or inline_urls

        degraded = bool(raw.get("degraded"))

        return SearchResult(
            source="x",
            title=str(raw.get("query", "")),
            url=all_urls[0] if all_urls else "",
            description=answer,
            metrics={"degraded": degraded},
            raw=raw,
            citations=all_urls,
            evidence_quality="inferred" if degraded else "cited",
            hallucination_flags=["unsourced"] if degraded else [],
        )
