def test_new_searchers_import_and_init():
    """Testa importação e inicialização dos novos searchers."""
    print("Testando importação dos novos searchers...")

    # 1. WikipediaSearcher
    from src.search.wikipedia_searcher import WikipediaSearcher
    wiki_config = {"lang": "en", "timeout": 10}
    wiki_searcher = WikipediaSearcher(wiki_config)
    assert wiki_searcher.lang == "en"
    assert wiki_searcher._base_url == "https://en.wikipedia.org"
    print("[OK] WikipediaSearcher importado e instanciado com sucesso")

    # 2. DuckDuckGoSearcher
    from src.search.duckduckgo_searcher import DuckDuckGoSearcher
    config = {}
    ddg_searcher = DuckDuckGoSearcher(config)
    assert ddg_searcher.source_name == "duckduckgo"
    assert ddg_searcher.base_url == "https://api.duckduckgo.com"
    print("[OK] DuckDuckGoSearcher importado e instanciado corretamente")

    # 3. PyPISearcher
    from src.search.pypi_searcher import PyPISearcher
    pypi_config = {}
    pypi_searcher = PyPISearcher(pypi_config)
    assert pypi_searcher.source_name == "pypi"
    assert pypi_searcher.base_url == "https://pypi.org"
    print("[OK] PyPISearcher importado e instanciado corretamente")

    # Testar registro na factory via @register_searcher
    from src.search.registry import get_registry, list_registered
    registry = get_registry()
    assert "wikipedia" in registry
    assert "duckduckgo" in registry
    assert "pypi" in registry
    print("[OK] Todos os novos searchers estão registrados via @register_searcher")

    print("\nTodos os testes de importação e inicialização passaram!")
    return True

if __name__ == "__main__":
    test_new_searchers_import_and_init()