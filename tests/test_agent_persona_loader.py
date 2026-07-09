"""Testes unitários para AgentPersonaLoader."""
import time
from pathlib import Path
import pytest
from unittest.mock import patch

from src.agent_persona_loader import AgentPersonaLoader


@pytest.fixture
def tmp_personas(tmp_path):
    """Cria um diretório temporário com personas de teste."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "sage_strategy.md").write_text(
        "---\nname: sage\n---\n\n# Sage\n\nConteúdo da persona Sage.",
        encoding="utf-8",
    )
    (agents_dir / "no_frontmatter.md").write_text(
        "# Sem frontmatter\n\nConteúdo direto.",
        encoding="utf-8",
    )
    return agents_dir


def test_load_existing_persona(tmp_personas):
    loader = AgentPersonaLoader(prompts_dir=tmp_personas)
    content = loader.load("sage_strategy")
    assert "# Sage" in content
    assert "---" not in content  # frontmatter removido


def test_load_nonexistent_returns_empty(tmp_personas):
    loader = AgentPersonaLoader(prompts_dir=tmp_personas)
    content = loader.load("nonexistent_agent")
    assert content == ""


def test_build_enhanced_prompt_injects_persona(tmp_personas):
    loader = AgentPersonaLoader(prompts_dir=tmp_personas)
    result = loader.build_enhanced_prompt("Prompt base.", "sage_strategy")
    assert "# Sage" in result
    assert "Prompt base." in result
    assert "---" in result  # divisor entre persona e prompt


def test_build_enhanced_prompt_passthrough_if_missing(tmp_personas):
    loader = AgentPersonaLoader(prompts_dir=tmp_personas)
    result = loader.build_enhanced_prompt("Prompt base.", "nonexistent")
    assert result == "Prompt base."


def test_cache_is_used_on_second_call(tmp_personas):
    loader = AgentPersonaLoader(prompts_dir=tmp_personas)
    loader.load("sage_strategy")
    # Remove o arquivo do disco
    (tmp_personas / "sage_strategy.md").unlink()
    # Segunda chamada deve usar cache (não falha com FileNotFoundError)
    content = loader.load("sage_strategy")
    assert "# Sage" in content


def test_clear_cache_forces_re_read(tmp_personas):
    loader = AgentPersonaLoader(prompts_dir=tmp_personas)
    loader.load("sage_strategy")
    loader.clear_cache()
    # Remove arquivo — agora deve retornar "" pois cache foi limpo
    (tmp_personas / "sage_strategy.md").unlink()
    content = loader.load("sage_strategy")
    assert content == ""


def test_strip_frontmatter_no_frontmatter(tmp_personas):
    loader = AgentPersonaLoader(prompts_dir=tmp_personas)
    content = loader.load("no_frontmatter")
    assert "# Sem frontmatter" in content


def test_is_persona_available(tmp_personas):
    loader = AgentPersonaLoader(prompts_dir=tmp_personas)
    assert loader.is_persona_available("sage_strategy") is True
    assert loader.is_persona_available("nonexistent") is False
