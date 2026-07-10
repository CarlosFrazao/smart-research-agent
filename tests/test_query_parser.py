"""Testes do parser de operadores de busca avançada (Fase 6, Tarefa 6.5)."""

from src.query_parser import ParsedQuery, parse_advanced_query


def test_parse_site_operator():
    """site: é extraído e removido do texto limpo."""
    parsed = parse_advanced_query("site:reddit.com best keyboard")
    assert parsed.site_filter == "reddit.com"
    assert parsed.text == "best keyboard"
    assert parsed.has_operators is True


def test_parse_filetype_operator():
    """filetype: é extraído e removido do texto limpo."""
    parsed = parse_advanced_query("filetype:pdf machine learning")
    assert parsed.filetype == "pdf"
    assert parsed.text == "machine learning"


def test_parse_intitle_operator():
    """intitle: é extraído e removido do texto limpo."""
    parsed = parse_advanced_query("intitle:python tutorial")
    assert parsed.intitle == "python"
    assert parsed.text == "tutorial"


def test_parse_multiple_operators_combined():
    """Múltiplos operadores são extraídos simultaneamente."""
    parsed = parse_advanced_query("site:github.com filetype:md fastapi guide")
    assert parsed.site_filter == "github.com"
    assert parsed.filetype == "md"
    assert parsed.text == "fastapi guide"


def test_parse_query_without_operators():
    """Query sem operadores devolve texto intacto e has_operators=False."""
    parsed = parse_advanced_query("melhor banco de dados 2026")
    assert parsed.site_filter is None
    assert parsed.filetype is None
    assert parsed.text == "melhor banco de dados 2026"
    assert parsed.has_operators is False


def test_parse_empty_query():
    """Query vazia/None não quebra o parser."""
    parsed = parse_advanced_query("")
    assert parsed.text == ""
    assert parsed.has_operators is False


def test_operators_are_case_insensitive():
    """Operadores funcionam independente de maiúsculas/minúsculas."""
    parsed = parse_advanced_query("SITE:reddit.com teste")
    assert parsed.site_filter == "reddit.com"
    assert parsed.text == "teste"


def test_to_engine_query_reattaches_operators():
    """to_engine_query reconstrói a query com operadores nativos."""
    parsed = parse_advanced_query("site:reddit.com best keyboard")
    engine_q = parsed.to_engine_query()
    assert "best keyboard" in engine_q
    assert "site:reddit.com" in engine_q


def test_to_engine_query_without_operators_equals_text():
    """Sem operadores, to_engine_query devolve apenas o texto."""
    parsed = parse_advanced_query("hello world")
    assert parsed.to_engine_query() == "hello world"


def test_returns_parsed_query_instance():
    """O retorno é sempre uma instância de ParsedQuery."""
    assert isinstance(parse_advanced_query("x"), ParsedQuery)
