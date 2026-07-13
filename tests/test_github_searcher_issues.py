"""Testes da Melhoria B+ do GitHubSearcher: busca inteligente de issues/PRs
de repositórios citados na query, contornando o limite do Code Search.

Segue TDD (Red → Green → Refactor). O objetivo é extrair o ``owner/repo``
citado na pergunta (ex.: ``github.com/kuzudb/kuzu``), traduzir termos
técnicos em português para inglês e consultar a API de issues do GitHub
(``repo:owner/repo is:issue <termos>``) como fallback enriquecido.
"""

import pytest

from src.search.github_searcher import GitHubSearcher


@pytest.fixture
def github_searcher():
    config = {"github_token": "dummy_token", "max_results": 10}
    return GitHubSearcher(config)


# ── _build_issues_query ───────────────────────────────────────────────────────


def test_dado_repo_e_termos_quando_build_issues_query_entao_formata_padrao(
    github_searcher,
):
    # Act
    q = github_searcher._build_issues_query("kuzudb/kuzu", "starvation msvcrt")
    # Assert
    assert q == "repo:kuzudb/kuzu is:issue starvation msvcrt"


def test_dado_repo_sem_termos_quando_build_issues_query_entao_apenas_repo(
    github_searcher,
):
    # Act
    q = github_searcher._build_issues_query("kuzudb/kuzu", "")
    # Assert
    assert q == "repo:kuzudb/kuzu is:issue"


# ── _extract_technical_terms ───────────────────────────────────────────────────


def test_dado_query_portugues_quando_extrair_termos_entao_mapeia_para_ingles(
    github_searcher,
):
    # Arrange — termos em PT que devem virar EN
    query = (
        "como resolver starvation de escrita na concorrência de leitores e "
        "escritores usando locks nativos"
    )
    # Act
    terms = github_searcher._extract_technical_terms(query)
    # Assert — mapeamento PT->EN deve ocorrer
    assert "starvation" in terms
    assert "concurrency" in terms
    assert "lock" in terms
    assert "writer" in terms or "reader" in terms


def test_dado_query_com_termos_ja_em_ingles_quando_extrair_entao_mantem(
    github_searcher,
):
    # Arrange
    query = "resolve write starvation with msvcrt fcntl locks on windows linux"
    # Act
    terms = github_searcher._extract_technical_terms(query)
    # Assert
    assert "msvcrt" in terms
    assert "fcntl" in terms
    assert "lock" in terms


def test_dado_query_vazia_quando_extrair_termos_entao_retorna_vazio(
    github_searcher,
):
    # Act
    terms = github_searcher._extract_technical_terms("")
    # Assert
    assert terms == ""


# ── search_repo_issues ─────────────────────────────────────────────────────────


def _fake_issue_item(issue_num: int, title: str, body: str = "") -> dict:
    return {
        "number": issue_num,
        "title": title,
        "html_url": f"https://github.com/kuzudb/kuzu/issues/{issue_num}",
        "body": body,
        "state": "open",
        "user": {"login": "contributor"},
        "created_at": "2025-12-01T00:00:00Z",
        "updated_at": "2026-01-15T00:00:00Z",
        "comments": 3,
        "pull_request": None,
    }


async def test_dado_repo_valido_quando_search_repo_issues_entao_retorna_normalizados(
    github_searcher, mocker
):
    # Arrange — resposta simulada da API de issues
    fake_items = [
        _fake_issue_item(2527, "Writer starvation with msvcrt lock on Windows"),
        _fake_issue_item(2530, "Use fcntl on Linux to avoid read lock starvation"),
    ]
    payload = {"total_count": len(fake_items), "items": fake_items}
    mock_get = mocker.patch.object(
        github_searcher.http, "get", mocker.AsyncMock(return_value=payload)
    )

    # Act
    results = await github_searcher.search_repo_issues("kuzudb/kuzu")

    # Assert — 2 resultados normalizados
    assert len(results) == 2
    assert results[0].source == "github_issues"
    assert results[0].url == "https://github.com/kuzudb/kuzu/issues/2527"
    assert "2527" in results[0].title
    # A chamada foi feita na URL de issues com o qualifier correto
    called_url = mock_get.call_args[0][0]
    assert called_url == "https://api.github.com/search/issues"
    called_params = mock_get.call_args.kwargs.get("params") or mock_get.call_args[1][1]
    assert called_params["q"].startswith("repo:kuzudb/kuzu is:issue")
    # Métricas populadas
    assert results[0].metrics["number"] == 2527
    assert "msvcrt" in results[0].description.lower() or "starvation" in results[0].title.lower()


async def test_dado_api_issues_retorna_vazio_quando_search_repo_issues_entao_lista_vazia(
    github_searcher, mocker
):
    # Arrange
    mock_get = mocker.patch.object(
        github_searcher.http, "get", mocker.AsyncMock(return_value={"items": []})
    )
    # Act
    results = await github_searcher.search_repo_issues("kuzudb/kuzu")
    # Assert
    assert results == []
    assert mock_get.call_args[0][0] == "https://api.github.com/search/issues"


async def test_dado_erro_http_quando_search_repo_issues_entao_retorna_vazio(
    github_searcher, mocker
):
    # Arrange
    mock_get = mocker.patch.object(
        github_searcher.http,
        "get",
        mocker.AsyncMock(side_effect=Exception("rate limit")),
    )
    # Act
    results = await github_searcher.search_repo_issues("kuzudb/kuzu")
    # Assert — falha degrada graciosamente, não quebra o pipeline
    assert results == []


# ── Integração no fluxo de busca (_do_search) ──────────────────────────────────


async def test_dado_zero_repos_e_repo_citado_quando_do_search_entao_ativa_issues(
    github_searcher, mocker
):
    # Arrange — code search e repositório retornam vazio (gatilho do fallback)
    mocker.patch.object(
        github_searcher.http, "get", mocker.AsyncMock(return_value={"items": []})
    )
    # search_repo_issues retorna issue real
    fake_issues = [_fake_issue_item(2527, "Writer starvation with msvcrt lock")]
    mocker.patch.object(
        github_searcher,
        "search_repo_issues",
        mocker.AsyncMock(return_value=github_searcher._normalize_issue_list(fake_issues)),
    )

    # Act — query citando o repo, domínio general
    results = await github_searcher._do_search(
        "kuzudb/kuzu sort:stars",
        "kuzudb kuzu",
        "sort:stars",
        "Analise o design de concorrência do banco de grafos KuzuDB "
        "(github.com/kuzudb/kuzu) sobre starvation de escrita usando locks msvcrt",
        {"Accept": "application/vnd.github.v3+json"},
        {"q": "kuzudb/kuzu sort:stars", "per_page": 10},
    )

    # Assert — o fallback de issues enriqueceu os resultados
    issue_results = [r for r in results if r.source == "github_issues"]
    assert issue_results, "fallback de issues deveria ter sido ativado"
    assert issue_results[0].metrics["number"] == 2527


async def test_dado_repos_encontrados_quando_do_search_entao_nao_chama_issues(
    github_searcher, mocker
):
    # Arrange — repositório encontrado (não deve acionar fallback de issues)
    repo_items = [
        {
            "full_name": "kuzudb/kuzu",
            "html_url": "https://github.com/kuzudb/kuzu",
            "description": "Embedded graph database",
            "stargazers_count": 5000,
            "forks_count": 300,
            "open_issues_count": 100,
            "language": "C++",
            "updated_at": "2026-01-01T00:00:00Z",
            "created_at": "2021-01-01T00:00:00Z",
            "license": None,
            "topics": ["database"],
            "watchers_count": 100,
        }
    ]
    mocker.patch.object(
        github_searcher.http, "get", mocker.AsyncMock(return_value={"items": repo_items})
    )
    mock_issues = mocker.patch.object(github_searcher, "search_repo_issues")

    # Act
    results = await github_searcher._do_search(
        "kuzudb/kuzu sort:stars",
        "kuzudb kuzu",
        "sort:stars",
        "github.com/kuzudb/kuzu",
        {"Accept": "application/vnd.github.v3+json"},
        {"q": "kuzudb/kuzu sort:stars", "per_page": 10},
    )

    # Assert — achou repo, não chamou fallback de issues
    assert any(r.source == "github" for r in results)
    mock_issues.assert_not_called()
