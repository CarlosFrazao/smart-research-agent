"""Testes para o dispatch implícito do subcomando `search` no CLI.

Garante que a invocação documentada `python -m cli.main "query" -m cirurgia`
(funciona sem a palavra `search` explícita) seja roteada corretamente para o
subcomando `search`, reduzindo erro de uso.
"""

from __future__ import annotations

from cli.main import ensure_search_subcommand


def test_implicit_search_when_query_is_first_arg():
    argv = ['"o que e ia"', "-m", "cirurgia", "-o", "out.md"]
    result = ensure_search_subcommand(argv)
    assert result[0] == "search"
    assert result[1:] == argv


def test_explicit_search_preserved():
    argv = ["search", "minha query", "-m", "radar"]
    result = ensure_search_subcommand(argv)
    assert result == argv


def test_known_subcommand_preserved():
    for cmd in ("schedule", "schedule-list", "schedule-run", "schedule-cancel", "status"):
        argv = [cmd, "arg"]
        assert ensure_search_subcommand(argv) == argv


def test_flags_preserved_without_insert():
    argv = ["--help"]
    assert ensure_search_subcommand(argv) == argv
    argv = ["-m", "cirurgia", "query"]
    # Flag na primeira posição -> não insere search (Typer resolverá).
    assert ensure_search_subcommand(argv) == argv


def test_empty_argv_unchanged():
    assert ensure_search_subcommand([]) == []
