"""Testes do Bloco 2 (E2-T1): Config Runtime via YAML para pesos e fontes.

Cobertura:
  1. `load_scoring_weights()` aplica `weights.bm25` no HybridRankerConfig.
  2. `load_scoring_weights()` ausente/inválido -> defaults hardcoded (sem exceção).
  3. `get_source_enabled()` respeita `enabled: false` em sources.yaml.
  4. `SearcherFactory` não instancia searchers desabilitados via YAML.

Os testes não tocam nos arquivos reais de `config/` — usam um diretório
temporário e limpam o `lru_cache` do loader para isolar cada caso.
"""

import src.config_loader

import pytest


def _make_loader_with_config(tmp_path, scoring_text=None, sources_text=None):
    """Aponta o `src.config_loader` real para um `config/` temporário.

    Não recarrega o módulo (para preservar a referência da função já importada
    por `hybrid_ranker`). Em vez disso, sobrescreve o atributo de módulo
    `CONFIG_DIR` e limpa os caches `lru_cache`, garantindo leitura isolada.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    if scoring_text is not None:
        (config_dir / "scoring_weights.yaml").write_text(scoring_text, encoding="utf-8")
    if sources_text is not None:
        (config_dir / "sources.yaml").write_text(sources_text, encoding="utf-8")

    import src.config_loader as cl

    cl.CONFIG_DIR = config_dir
    cl.load_scoring_weights.cache_clear()
    cl.load_sources_config.cache_clear()
    return cl


# ── 1. YAML weights aplicados no HybridRankerConfig ────────────────────────


def test_yaml_bm25_weight_applied(tmp_path):
    cl = _make_loader_with_config(
        tmp_path,
        scoring_text="weights:\n  bm25: 0.40\n  embedding: 0.20\n  heuristic: 0.30\n  llm: 0.10\n",
    )
    from src.ranking.hybrid_ranker import HybridRankerConfig

    cfg = HybridRankerConfig()  # dispara __post_init__ -> _apply_yaml_weights
    assert cfg.bm25_weight == pytest.approx(0.40)
    assert cfg.embedding_weight == pytest.approx(0.20)
    assert cfg.heuristic_weight == pytest.approx(0.30)
    assert cfg.llm_weight == pytest.approx(0.10)


def test_yaml_partial_weight_override(tmp_path):
    """Apenas `bm25` sobrescrito; demais pesos mantêm defaults hardcoded.

    Os pesos abaixo somam 1.0 exato, evitando a normalização automática do
    HybridRankerConfig (que ocorre quando o total desvia > 0.01 do esperado).
    """
    cl = _make_loader_with_config(
        tmp_path,
        scoring_text=(
            "weights:\n"
            "  bm25: 0.50\n"  # sobrescrito
            "  embedding: 0.20\n"  # sobrescrito
            "  heuristic: 0.25\n"  # == default (preservado)
            "  llm: 0.05\n"  # sobrescrito
        ),
    )
    from src.ranking.hybrid_ranker import (
        DEFAULT_HEURISTIC_WEIGHT,
        HybridRankerConfig,
    )

    cfg = HybridRankerConfig()
    assert cfg.bm25_weight == pytest.approx(0.50)
    assert cfg.embedding_weight == pytest.approx(0.20)
    # Default hardcoded preservado quando o valor no YAML == default
    assert cfg.heuristic_weight == pytest.approx(DEFAULT_HEURISTIC_WEIGHT)
    assert cfg.llm_weight == pytest.approx(0.05)


def test_yaml_weights_auto_normalize_when_sum_not_one(tmp_path):
    """Soma != 1.0 dispara normalização automática (com warning)."""
    cl = _make_loader_with_config(
        tmp_path,
        scoring_text="weights:\n  bm25: 0.55\n",  # resto default -> total 1.25
    )
    from src.ranking.hybrid_ranker import HybridRankerConfig

    cfg = HybridRankerConfig()
    total = (
        cfg.bm25_weight + cfg.embedding_weight + cfg.heuristic_weight + cfg.llm_weight
    )
    assert total == pytest.approx(1.0)  # normalizado
    assert cfg.bm25_weight == pytest.approx(0.55 / 1.25)


# ── 2. YAML ausente/inválido -> defaults sem exceção ───────────────────────


def test_missing_scoring_weights_uses_defaults(tmp_path):
    cl = _make_loader_with_config(tmp_path)  # nenhum arquivo criado
    from src.ranking.hybrid_ranker import (
        DEFAULT_BM25_WEIGHT,
        DEFAULT_EMBEDDING_WEIGHT,
        DEFAULT_HEURISTIC_WEIGHT,
        DEFAULT_LLM_WEIGHT,
        HybridRankerConfig,
    )

    cfg = HybridRankerConfig()
    assert cfg.bm25_weight == pytest.approx(DEFAULT_BM25_WEIGHT)
    assert cfg.embedding_weight == pytest.approx(DEFAULT_EMBEDDING_WEIGHT)
    assert cfg.heuristic_weight == pytest.approx(DEFAULT_HEURISTIC_WEIGHT)
    assert cfg.llm_weight == pytest.approx(DEFAULT_LLM_WEIGHT)


def test_invalid_scoring_weights_uses_defaults(tmp_path):
    cl = _make_loader_with_config(
        tmp_path,
        scoring_text="weights: [1, 2, 3]\n",  # não é mapping -> ignorado
    )
    from src.ranking.hybrid_ranker import (
        DEFAULT_BM25_WEIGHT,
        HybridRankerConfig,
    )

    cfg = HybridRankerConfig()
    assert cfg.bm25_weight == pytest.approx(DEFAULT_BM25_WEIGHT)


def test_loader_returns_empty_dict_when_file_absent(tmp_path):
    cl = _make_loader_with_config(tmp_path)
    assert cl.load_scoring_weights() == {}
    assert cl.load_sources_config() == {}


# ── 3. get_source_enabled respeita enabled:false ───────────────────────────


def test_get_source_enabled_false(tmp_path):
    cl = _make_loader_with_config(
        tmp_path,
        sources_text="sources:\n  github:\n    enabled: false\n  reddit:\n    enabled: true\n",
    )
    assert cl.get_source_enabled("github") is False
    assert cl.get_source_enabled("reddit") is True


def test_get_source_enabled_default_when_absent(tmp_path):
    cl = _make_loader_with_config(tmp_path)
    # Sem arquivo -> comportamento padrão (True, preserva boot atual)
    assert cl.get_source_enabled("github") is True
    assert cl.get_source_enabled("nonexistent", default=False) is False


# ── 4. SearcherFactory não instancia fonte desabilitada ────────────────────


@pytest.fixture
def _patch_config_loader(tmp_path):
    """Força get_source_enabled() da factory a ler um sources.yaml temporário.

    A factory importa `get_source_enabled` DENTRO da função (lazy import), então
    não adianta patchear o atributo do módulo `factory`. Em vez disso, sobrescrevemos
    `CONFIG_DIR` + limpamos o cache do loader real — a factory chamará a mesma
    função e lerá o YAML temporário. Restauramos o `CONFIG_DIR` original ao fim.
    """
    import src.config_loader as cl

    original_dir = cl.CONFIG_DIR
    _make_loader_with_config(
        tmp_path,
        sources_text="sources:\n  github:\n    enabled: false\n",
    )
    yield cl
    cl.CONFIG_DIR = original_dir
    cl.load_sources_config.cache_clear()


def test_factory_skips_disabled_source(_patch_config_loader):
    from unittest.mock import MagicMock

    from src.search.factory import SearcherFactory

    mock_config = MagicMock()
    mock_config.notion_api_key = None
    mock_config.confluence_api_key = None
    mock_config.confluence_base_url = None
    mock_config.confluence_username = None
    mock_config.sharepoint_client_id = None
    mock_config.sharepoint_client_secret = None
    mock_config.sharepoint_tenant_id = None
    mock_config.firecrawl_api_key = None
    mock_config.firecrawl_base_url = None
    mock_config.spider_api_key = None
    mock_config.spider_base_url = None
    mock_config.steel_api_key = None
    mock_config.steel_base_url = None
    mock_config.host_mode = False
    mock_config.spider_enabled = False
    mock_config.steel_enabled = False
    mock_config.playwright_enabled = False
    mock_config.residential_proxy_provider = None
    mock_config.semantic_scholar_api_key = None
    mock_config.ncbi_api_key = None
    mock_config.youtube_api_key = None
    mock_config.serpapi_api_key = None
    mock_config.serpapi_enabled = False
    mock_config.jina_reader_base_url = "https://r.jina.ai/"

    mock_orchestrator = MagicMock()
    mock_orchestrator.config = mock_config
    mock_orchestrator.llm = None

    searchers = SearcherFactory.create_searchers(mock_orchestrator)
    assert "github" not in searchers
    # Fonte não desabilitada continua presente
    assert "reddit" in searchers
