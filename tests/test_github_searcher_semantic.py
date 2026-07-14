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

def test_dado_query_citando_repo_github_quando_build_code_query_entao_usa_repo_qualifier(github_searcher):
    # Arrange
    query = (
        "Analise o design de concorrência multithread do banco de grafos KuzuDB "
        "no repositório oficial (github.com/kuzudb/kuzu) sobre starvation de escrita"
    )
    # Act
    code_q = github_searcher._build_code_query(query)
    # Assert
    assert code_q.startswith("repo:kuzudb/kuzu ")
    # Deve conter termos técnicos relevantes extraídos, em ASCII
    assert "concorrencia" in code_q or "starvation" in code_q or "kuzudb" in code_q
    # Não deve conter a URL completa nem parênteses
    assert "github.com" not in code_q
    assert "(" not in code_q

def test_dado_query_citando_repo_curto_quando_build_code_query_entao_extrai_owner_repo(github_searcher):
    # Arrange
    query = "discussoes no repo kuzudb/kuzu sobre locks nativos msvcrt fcntl"
    # Act
    code_q = github_searcher._build_code_query(query)
    # Assert
    assert code_q.startswith("repo:kuzudb/kuzu ")
    assert "msvcrt" in code_q or "fcntl" in code_q or "lock" in code_q

def test_dado_query_sem_repo_quando_build_code_query_entao_normaliza_texto(github_searcher):
    # Arrange
    query = "Analise o design de concorrência multithread do banco de grafos KuzuDB em Python"
    # Act
    code_q = github_searcher._build_code_query(query)
    # Assert
    assert not code_q.startswith("repo:")
    # Texto normalizado em ASCII, sem acentos
    assert "concorrencia" in code_q
    assert "github.com" not in code_q
