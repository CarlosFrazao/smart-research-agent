import pytest
from datetime import datetime, timedelta
from src.search.github_searcher import GitHubSearcher

@pytest.fixture
def github_searcher():
    config = {
        "github_token": "dummy_token",
        "max_results": 10
    }
    return GitHubSearcher(config)

def test_dado_query_com_rust_quando_extrair_filtros_entao_identifica_linguagem_rust(github_searcher):
    # Act
    filters = github_searcher._extract_semantic_filters("Which GitHub repositories implementing MCP server Rust")
    # Assert
    assert filters.get("language") == "rust"

def test_dado_query_com_javascript_quando_extrair_filtros_entao_identifica_linguagem_javascript(github_searcher):
    # Act
    filters = github_searcher._extract_semantic_filters("best javascript testing tools")
    # Assert
    assert filters.get("language") == "javascript"

def test_dado_query_com_last_30_days_quando_extrair_filtros_entao_identifica_data_criacao(github_searcher):
    # Act
    filters = github_searcher._extract_semantic_filters("Rust MCP server last 30 days")
    # Assert
    assert "created" in filters
    assert filters["created"].startswith(">")
    # Validar que a data calculada está próxima a 30 dias atrás
    date_str = filters["created"].split(">")[1]
    parsed_date = datetime.strptime(date_str, "%Y-%m-%d")
    expected_date = datetime.now() - timedelta(days=30)
    assert abs((parsed_date - expected_date).days) <= 1

def test_dado_query_com_last_2_weeks_quando_extrair_filtros_entao_identifica_data_criacao_duas_semanas(github_searcher):
    # Act
    filters = github_searcher._extract_semantic_filters("Rust MCP server last 2 weeks")
    # Assert
    assert "created" in filters
    date_str = filters["created"].split(">")[1]
    parsed_date = datetime.strptime(date_str, "%Y-%m-%d")
    expected_date = datetime.now() - timedelta(weeks=2)
    assert abs((parsed_date - expected_date).days) <= 1

def test_dado_query_com_estrelas_quando_extrair_filtros_entao_identifica_estrelas_corretamente(github_searcher):
    # Act
    filters = github_searcher._extract_semantic_filters("MCP server Rust more than 100 stars")
    # Assert
    assert filters.get("stars") == ">100"

def test_dado_query_com_simbolo_maior_quando_extrair_filtros_entao_identifica_estrelas_corretamente(github_searcher):
    # Act
    filters = github_searcher._extract_semantic_filters("MCP server Rust >500 stars")
    # Assert
    assert filters.get("stars") == ">500"

def test_dado_query_com_mcp_quando_extrair_filtros_entao_identifica_mcp_como_topic(github_searcher):
    # Act
    filters = github_searcher._extract_semantic_filters("mcp server rust")
    # Assert
    assert filters.get("topic") == "mcp"

def test_dado_query_generica_quando_extrair_filtros_entao_retorna_dicionario_vazio(github_searcher):
    # Act
    filters = github_searcher._extract_semantic_filters("best tools and libraries")
    # Assert
    assert filters == {}
