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


# ── M3.2 — reparo de JSON truncado (LLM local cortado no limite de tokens) ──


def test_truncated_array_with_dangling_object() -> None:
    parsed = LLMClient._safe_parse_json('[{"a": 1}, {"b": 2}, {"c":')
    assert parsed == [{"a": 1}, {"b": 2}]


def test_truncated_array_missing_closing_bracket() -> None:
    parsed = LLMClient._safe_parse_json('[{"a": 1}, {"b": 2}')
    assert parsed == [{"a": 1}, {"b": 2}]


def test_truncated_open_string_value() -> None:
    parsed = LLMClient._safe_parse_json('{"name": "partial value that got cut')
    assert parsed == {"name": "partial value that got cut"}


def test_truncated_nested_array() -> None:
    parsed = LLMClient._safe_parse_json('{"items": [1, 2, 3')
    assert parsed == {"items": [1, 2, 3]}


def test_truncated_key_without_value() -> None:
    parsed = LLMClient._safe_parse_json('{"a": 1, "b":')
    assert parsed == {"a": 1}


def test_truncated_with_prose_prefix() -> None:
    # Recuperação graciosa: extrai JSON válido de texto com prefixo de prosa +
    # truncamento. Aceita tanto o array reparado quanto o 1º objeto válido —
    # ambos são recuperações corretas (o essencial é NÃO retornar None).
    parsed = LLMClient._safe_parse_json('Here is the JSON: [{"x": 1}, {"y":')
    assert parsed in ([{"x": 1}], {"x": 1})
    assert parsed is not None


def test_truncation_garbage_still_none() -> None:
    assert LLMClient._safe_parse_json("completely not json at all") is None


# ── M3.2 — few-shot skeleton do schema ──


def test_schema_example_array_of_objects() -> None:
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "weight": {"type": "number"}},
        },
    }
    ex = LLMClient._schema_example(schema)
    assert ex == '[{"query": "...", "weight": 0}]'


def test_schema_example_object() -> None:
    schema = {
        "type": "object",
        "properties": {
            "domain": {"type": "string"},
            "urgency": {"type": "boolean"},
        },
    }
    ex = LLMClient._schema_example(schema)
    assert '"domain": "..."' in ex
    assert '"urgency": true' in ex
