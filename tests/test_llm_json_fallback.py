"""
Testes para o fallback robusto de JSON em generate_structured (HIPO do deep mode).
Valida que _safe_parse_json extrai JSON válido de texto sujo e faz retry quando necessário.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.clients.llm_client import LLMClient, LLMProvider


class TestSafeParseJson:
    """Testes do método _safe_parse_json (extract_json_blob aprimorado)."""

    def test_valid_json_object(self):
        """JSON puro de objeto deve ser parseado diretamente."""
        text = '{"key": "value", "number": 42}'
        result = LLMClient._safe_parse_json(text)
        assert result == {"key": "value", "number": 42}

    def test_valid_json_array(self):
        """JSON puro de array deve ser parseado diretamente."""
        text = '[{"id": 1}, {"id": 2}]'
        result = LLMClient._safe_parse_json(text)
        assert result == [{"id": 1}, {"id": 2}]

    def test_json_with_markdown_fences(self):
        """JSON envolto em ```json``` deve ser extraído."""
        text = '```json\n{"key": "value"}\n```'
        result = LLMClient._safe_parse_json(text)
        assert result == {"key": "value"}

    def test_json_with_generic_fences(self):
        """JSON envolto em ``` deve ser extraído."""
        text = '```\n{"items": [1, 2, 3]}\n```'
        result = LLMClient._safe_parse_json(text)
        assert result == {"items": [1, 2, 3]}

    def test_json_embedded_in_text(self):
        """JSON no meio de texto deve ser extraído via regex."""
        text = 'Here is the answer: {"result": "found"}. Hope this helps!'
        result = LLMClient._safe_parse_json(text)
        assert result == {"result": "found"}

    def test_no_json_returns_none(self):
        """Texto sem JSON retorna None (não estoura exceção)."""
        text = "This is just plain text with no JSON content."
        result = LLMClient._safe_parse_json(text)
        assert result is None

    def test_empty_string_returns_none(self):
        """String vazia retorna None."""
        result = LLMClient._safe_parse_json("")
        assert result is None

    def test_array_embedded_in_text(self):
        """Array JSON no meio de texto deve ser extraído."""
        text = 'Results: [{"a": 1}, {"a": 2}] and nothing else.'
        result = LLMClient._safe_parse_json(text)
        assert result == [{"a": 1}, {"a": 2}]

    def test_nested_json_extraction(self):
        """JSON aninhado profundo deve ser extraído corretamente."""
        text = '{"outer": {"inner": {"value": "deep"}}}'
        result = LLMClient._safe_parse_json(text)
        assert result == {"outer": {"inner": {"value": "deep"}}}


class TestGenerateStructuredFallback:
    """Testes do generate_structured com fallback robusto."""

    @pytest.mark.asyncio
    async def test_generate_structured_successful_json(self):
        """generate_structured retorna JSON parseado quando LLM devolve JSON válido."""
        client = MagicMock(spec=LLMClient)
        client.max_repair_attempts = 1
        client.last_failure = None
        client._safe_parse_json = LLMClient._safe_parse_json

        # Mock da chamada generate
        client.generate = AsyncMock(return_value='[{"item": "value"}]')

        # Chama o método real (bind manual)
        bound = LLMClient.generate_structured.__get__(client)
        result = await bound(prompt="test", schema={"type": "array"})
        assert result == [{"item": "value"}]

    @pytest.mark.asyncio
    async def test_generate_structured_with_markdown_json(self):
        """generate_structured extrai JSON envolto em markdown."""
        client = MagicMock(spec=LLMClient)
        client.max_repair_attempts = 1
        client._safe_parse_json = LLMClient._safe_parse_json

        # LLM retorna JSON com markdown
        markdown_json = '```json\n{"key": "extracted"}\n```'
        client.generate = AsyncMock(return_value=markdown_json)

        bound = LLMClient.generate_structured.__get__(client)
        result = await bound(prompt="test", schema={"type": "object"})
        assert result == {"key": "extracted"}

    @pytest.mark.asyncio
    async def test_generate_structured_non_json_fallback(self):
        """generate_structured retorna fallback quando LLM não devolve JSON."""
        client = MagicMock(spec=LLMClient)
        client.max_repair_attempts = 1
        client._safe_parse_json = LLMClient._safe_parse_json

        # LLM retorna texto não-JSON (2 tentativas)
        client.generate = AsyncMock(side_effect=[
            "I cannot produce JSON right now.",
            "Still not JSON, sorry!"
        ])

        bound = LLMClient.generate_structured.__get__(client)
        result = await bound(prompt="test", schema={"type": "array"})
        # Deve retornar fallback seguro (array vazio)
        assert result == []

    @pytest.mark.asyncio
    async def test_generate_structured_non_json_object_fallback(self):
        """generate_structured retorna fallback object {} quando schema é object."""
        client = MagicMock(spec=LLMClient)
        client.max_repair_attempts = 1
        client._safe_parse_json = LLMClient._safe_parse_json

        client.generate = AsyncMock(return_value="No JSON here!")

        bound = LLMClient.generate_structured.__get__(client)
        result = await bound(prompt="test", schema={"type": "object"})
        assert result == {}

    @pytest.mark.asyncio
    async def test_generate_structured_sets_last_failure(self):
        """generate_structured deve registrar last_failure quando falha definitiva."""
        client = MagicMock(spec=LLMClient)
        client.max_repair_attempts = 1
        client._safe_parse_json = LLMClient._safe_parse_json

        client.generate = AsyncMock(return_value="Not JSON at all")

        bound = LLMClient.generate_structured.__get__(client)
        await bound(prompt="test", schema={"type": "array"})
        assert client.last_failure is not None
        assert "falha definitiva" in client.last_failure


class TestExtractJsonBlob:
    """Testes do _extract_json_blob (já existente, coberto por _safe_parse_json)."""

    def test_extract_from_fence(self):
        """Extrai JSON de code fence padrão."""
        text = '```json\n{"test": true}\n```'
        result = LLMClient._extract_json_blob(text)
        assert result is not None
        import json
        parsed = json.loads(result)
        assert parsed == {"test": True}

    def test_extract_array_from_fence(self):
        """Extrai array JSON de code fence."""
        text = '```\n[1, 2, 3]\n```'
        result = LLMClient._extract_json_blob(text)
        assert result is not None
        import json
        parsed = json.loads(result)
        assert parsed == [1, 2, 3]


class TestRepairCascadeWiring:
    """Conexão explícita de _repair_json_defects e _repair_truncated_json
    dentro de _safe_parse_json (cascata: direto → bloco → defeitos → truncado).
    """

    # ── Reparo de defeitos comuns ────────────────────────────────────────────

    def test_trailing_comma_array_recovered(self):
        assert LLMClient._safe_parse_json('[{"a": 1,}, {"b": 2,},]') == [
            {"a": 1},
            {"b": 2},
        ]

    def test_trailing_comma_object_recovered(self):
        assert LLMClient._safe_parse_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}

    def test_smart_quotes_recovered(self):
        assert LLMClient._safe_parse_json("{“key”: “val”}") == {"key": "val"}

    # ── Reparo de JSON truncado (cortado no limite de tokens) ────────────────

    def test_truncated_array_dangling_object(self):
        assert LLMClient._safe_parse_json('[{"a": 1}, {"b": 2}, {"c":') == [
            {"a": 1},
            {"b": 2},
        ]

    def test_truncated_missing_closing_bracket(self):
        assert LLMClient._safe_parse_json('[{"a": 1}, {"b": 2}') == [
            {"a": 1},
            {"b": 2},
        ]

    def test_truncated_open_string(self):
        assert LLMClient._safe_parse_json('{"name": "cut off here') == {
            "name": "cut off here"
        }

    def test_truncated_nested_array(self):
        assert LLMClient._safe_parse_json('{"items": [1, 2, 3') == {
            "items": [1, 2, 3]
        }

    def test_truncated_key_without_value(self):
        assert LLMClient._safe_parse_json('{"a": 1, "b":') == {"a": 1}

    # ── Negativos ────────────────────────────────────────────────────────────

    def test_pure_garbage_returns_none(self):
        assert LLMClient._safe_parse_json("isto nao e json de forma alguma") is None

    def test_none_input_returns_none(self):
        assert LLMClient._safe_parse_json(None) is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ('{"a": 1}', {"a": 1}),
            ('[{"a": 1,},]', [{"a": 1}]),
            ('{"items": [1, 2', {"items": [1, 2]}),
            ("lixo total", None),
        ],
    )
    def test_cascade_parametrized(self, raw, expected):
        assert LLMClient._safe_parse_json(raw) == expected
