"""Regressão do BUG C — reparo de defeitos comuns de JSON de LLMs locais.

Cobre ``LLMClient._repair_json_defects`` / ``_safe_parse_json``, que agora
recuperam JSON com vírgulas à direita e aspas tipográficas em vez de cair
no fallback seguro (que zerava expansões de query e seções de relatório).

Ver: sessão 2026-07-16 (teste de estresse black_ops).
"""

from src.clients.llm_client import LLMClient


def test_trailing_comma_in_array_is_repaired() -> None:
    parsed = LLMClient._safe_parse_json('[{"a": 1,}, {"b": 2,},]')
    assert parsed == [{"a": 1}, {"b": 2}]


def test_smart_quotes_are_repaired() -> None:
    # Aspas tipográficas do modelo local, com prefixo de prosa.
    parsed = LLMClient._safe_parse_json("json {“key”: “val”}")
    assert parsed == {"key": "val"}


def test_clean_json_still_parses() -> None:
    assert LLMClient._safe_parse_json('{"ok": true}') == {"ok": True}


def test_json_inside_markdown_fence() -> None:
    text = '```json\n{"x": [1, 2, 3]}\n```'
    assert LLMClient._safe_parse_json(text) == {"x": [1, 2, 3]}


def test_pure_garbage_returns_none() -> None:
    assert LLMClient._safe_parse_json("no json here at all") is None


def test_empty_returns_none() -> None:
    assert LLMClient._safe_parse_json("") is None


def test_repair_is_idempotent_on_valid_json() -> None:
    blob = '{"a": 1}'
    assert LLMClient._repair_json_defects(blob) == blob
