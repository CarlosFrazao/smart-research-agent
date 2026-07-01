"""Testes unitários do hook anti-query-vaga."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

# Importa diretamente a lógica pura (sem I/O)
sys.path.insert(0, str(Path(__file__).parent.parent))
from hooks.anti_query_vaga import analyze


# ── analyze() — lógica pura ───────────────────────────────────────────────────

class TestAnalyzeApproved:
    def test_query_clara_aprovada(self):
        flagged, _ = analyze("CRM open source parecido com HubSpot", [])
        assert not flagged

    def test_slash_command_aprovado(self):
        flagged, _ = analyze("/research_technology CRM open source", [])
        assert not flagged

    def test_pergunta_com_interrogacao_aprovada(self):
        flagged, _ = analyze("qual o melhor CRM open source?", [])
        assert not flagged

    def test_pergunta_com_como_aprovada(self):
        flagged, _ = analyze("como configurar n8n self-hosted", [])
        assert not flagged

    def test_ok_curto_aprovado(self):
        flagged, _ = analyze("ok", [])
        assert not flagged

    def test_sim_curto_aprovado(self):
        flagged, _ = analyze("sim", [])
        assert not flagged

    def test_continue_aprovado(self):
        flagged, _ = analyze("continue", [])
        assert not flagged

    def test_query_longa_sem_interrogacao_aprovada(self):
        flagged, _ = analyze("melhores ferramentas self-hosted para RAG com LLM em 2026", [])
        assert not flagged

    def test_what_em_ingles_aprovado(self):
        flagged, _ = analyze("what are the best open source CRM tools", [])
        assert not flagged


class TestAnalyzeFlagged:
    def test_query_muito_curta(self):
        flagged, reasons = analyze("CRM", [])
        assert flagged
        assert any("curta" in r for r in reasons)

    def test_palavra_vaga_pesquisa(self):
        flagged, reasons = analyze("pesquisa", [])
        assert flagged
        assert any("vaga" in r for r in reasons)

    def test_palavra_vaga_busca(self):
        flagged, reasons = analyze("busca", [])
        assert flagged

    def test_palavra_vaga_isso(self):
        flagged, reasons = analyze("isso", [])
        assert flagged

    def test_palavra_vaga_ne(self):
        flagged, reasons = analyze("né", [])
        assert flagged

    def test_error_paste_sem_pergunta(self):
        prompt = (
            "Traceback (most recent call last):\n"
            "  File 'main.py', line 10, in <module>\n"
            "    result = client.get()\n"
            "AttributeError: 'NoneType' object has no attribute 'get'\n"
        )
        flagged, reasons = analyze(prompt, [])
        assert flagged
        assert any("stack trace" in r for r in reasons)

    def test_error_paste_com_pergunta_nao_flagged(self):
        prompt = (
            "Traceback (most recent call last):\n"
            "  File 'main.py', line 10\n"
            "AttributeError: NoneType\n"
            "Como resolvo esse AttributeError?"
        )
        flagged, _ = analyze(prompt, [])
        assert not flagged

    def test_retry_curto_sem_contexto(self):
        flagged, reasons = analyze("tenta de novo", [])
        assert flagged
        assert any("tenta de novo" in r for r in reasons)

    def test_retry_ingles(self):
        flagged, reasons = analyze("try again", [])
        assert flagged

    def test_loop_prompt_repetido(self):
        history = ["CRM open source", "CRM open source"]
        flagged, reasons = analyze("CRM open source", history)
        assert flagged
        assert any("idêntica" in r for r in reasons)

    def test_loop_nao_flagged_query_nova(self):
        history = ["CRM open source"]
        flagged, _ = analyze("alternativas ao Zapier para automação", history)
        assert not flagged

    def test_error_paste_dois_erros(self):
        prompt = (
            "File 'x.py', line 5\n"
            "TypeError: unsupported operand\n"
            "ValueError: invalid literal\n"
            "KeyError: 'key'\n"
        )
        flagged, _ = analyze(prompt, [])
        assert flagged


class TestAnalyzeReasons:
    def test_reasons_nao_vazias_quando_flagged(self):
        flagged, reasons = analyze("isso", [])
        assert flagged
        assert len(reasons) > 0
        assert all(isinstance(r, str) and len(r) > 0 for r in reasons)

    def test_multiple_reasons_acumulam(self):
        # "tenta de novo" é curto E é padrão retry
        flagged, reasons = analyze("refaz", [])
        assert flagged
        assert len(reasons) >= 1


# ── main() via subprocess — testa o protocolo JSON ───────────────────────────

HOOK_PATH = str(Path(__file__).parent.parent / "hooks" / "anti_query_vaga.py")


def _run_hook(prompt: str) -> dict:
    payload = json.dumps({"prompt": prompt})
    result = subprocess.run(
        [sys.executable, HOOK_PATH],
        input=payload,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return json.loads(result.stdout.strip())


def test_hook_approves_good_query():
    out = _run_hook("melhores frameworks Python para APIs REST em 2026")
    assert out["decision"] == "approve"


def test_hook_blocks_vague_query():
    out = _run_hook("pesquisa")
    assert out["decision"] == "block"
    assert "reason" in out
    assert len(out["reason"]) > 20


def test_hook_blocks_short_query():
    out = _run_hook("CRM")
    assert out["decision"] == "block"


def test_hook_approves_slash_command():
    out = _run_hook("/research_technology CRM open source")
    assert out["decision"] == "approve"


def test_hook_approves_question():
    out = _run_hook("como configurar Docker para n8n self-hosted?")
    assert out["decision"] == "approve"


def test_hook_empty_input_approves():
    payload = json.dumps({})
    result = subprocess.run(
        [sys.executable, HOOK_PATH],
        input=payload,
        capture_output=True,
        text=True,
        timeout=5,
    )
    out = json.loads(result.stdout.strip())
    assert out["decision"] == "approve"


def test_hook_invalid_json_approves():
    result = subprocess.run(
        [sys.executable, HOOK_PATH],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=5,
    )
    out = json.loads(result.stdout.strip())
    assert out["decision"] == "approve"
