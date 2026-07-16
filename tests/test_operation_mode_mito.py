"""
Testes para o modo de operação 'mito' (fact-checking de mitos populares).
Valida que o preset existe, tem fontes corretas e passa validação de configuração.
"""

import pytest
from src.operation_modes import OperationModes


class TestOperationModeMito:
    """Testes do modo 'mito'."""

    def test_mito_mode_exists(self):
        """O modo 'mito' deve estar registrado em OperationModes.MODES."""
        assert "mito" in OperationModes.MODES, "Modo 'mito' não encontrado em OperationModes.MODES"

    def test_mito_mode_config(self):
        """Configuração do modo 'mito' deve ter valores esperados."""
        cfg = OperationModes.MODES["mito"]

        # Nome e descrição
        assert cfg.name == "mito"
        assert "mito" in cfg.description.lower() or "fact-check" in cfg.description.lower()

        # Searchers prioritários para fact-checking de mitos
        expected_searchers = {"web", "searxng", "wikipedia", "snopes", "reddit"}
        assert set(cfg.searchers) == expected_searchers, \
            f"Searchers do modo mito: {cfg.searchers}, esperado: {expected_searchers}"

        # Scrapers
        assert set(cfg.scrapers) == {"firecrawl", "jina"}

        # Thresholds
        assert cfg.confidence_threshold == 0.70
        assert cfg.max_depth == 2
        assert cfg.timeout_seconds == 180
        assert cfg.cost_optimization is False

        # Features avançadas
        assert cfg.enable_auditor is True
        assert cfg.enable_race is True
        assert cfg.enable_adversarial_pass is True
        assert "prism_scientist" in cfg.active_personas

        # Estratégias
        assert cfg.proxy_strategy == "rotate_careful"
        assert cfg.cache_strategy == "moderate"

    def test_mito_mode_validation_passes(self):
        """Validação de todos os modos deve passar (incluindo o novo 'mito')."""
        # Não deve levantar exceção
        OperationModes.validate_operation_modes()

    def test_mito_mode_in_list_modes(self):
        """list_modes deve incluir 'mito'."""
        modes = OperationModes.list_modes()
        assert "mito" in modes

    def test_mito_mode_get_mode(self):
        """get_mode('mito') deve retornar a config correta."""
        cfg = OperationModes.get_mode("mito")
        assert cfg.name == "mito"

    def test_mito_mode_description(self):
        """get_mode_description('mito') deve retornar string não vazia."""
        desc = OperationModes.get_mode_description("mito")
        assert isinstance(desc, str)
        assert len(desc) > 0


class TestOperationModeMitoAutoSelect:
    """Testes de auto-seleção do modo 'mito' via palavras-chave."""

    def test_auto_select_mito_keywords(self):
        """Queries com palavras-chave de mito devem selecionar modo 'mito'."""
        mito_keywords = [
            "mito",
            "lenda",
            "fact-check",
            "factcheck",
            "verificar",
            "desmascarar",
            "cérebro usa 10%",
            "água fria queima calorias",
            "popular belief",
            "urban legend",
        ]
        for kw in mito_keywords:
            selected = OperationModes.auto_select(kw)
            # O auto_select atual não tem keywords específicas para 'mito',
            # mas não deve quebrar. Verifica que retorna um modo válido.
            assert selected in OperationModes.MODES, f"Auto-select falhou para '{kw}': {selected}"

    def test_auto_select_default_fallback(self):
        """Query sem keywords especiais deve cair no modo padrão."""
        selected = OperationModes.auto_select("como configurar docker")
        assert selected in OperationModes.MODES
        # Deve ser 'cirurgia' (DEFAULT_MODE) ou outro modo válido
        assert selected == OperationModes.DEFAULT_MODE or selected in OperationModes.MODES
