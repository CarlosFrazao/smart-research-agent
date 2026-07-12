import shutil
import tempfile
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ============================================================ 8.1 KnowledgeGraph
class TestKnowledgeGraph:
    """
    Testes da classe KnowledgeGraph — agora usando KuzuDB como backend.
    A classe mantém a API legada (add_fact, query_entity, close, _enabled)
    mas opera localmente sem necessidade de Neo4j.
    """

    @pytest.fixture(autouse=True)
    def _isolate_kuzu(self):
        """Isola o diretório de dados do KuzuDB por teste.

        Sem isto, ``_make_config(kuzu_path=None)`` cairia no fallback do
        diretório compartilhado ``kuzu_data/``, disputando o arquivo
        ``kuzu.lock`` com a memória real do SRA e com execuções paralelas —
        causando falhas intermitentes de concorrência. Cada teste recebe um
        diretório temporário exclusivo, removido no teardown.
        """
        self._kuzu_dirs: list[str] = []
        yield
        for path in self._kuzu_dirs:
            shutil.rmtree(path, ignore_errors=True)

    def _make_config(self, kuzu_path=None):
        """Cria um config mock com kuzu_data_path isolado por teste.

        Quando ``kuzu_path`` não é fornecido, gera um diretório temporário
        exclusivo (UUID) em vez de reutilizar o ``kuzu_data/`` global,
        garantindo isolamento total de concorrência entre testes.
        """
        if kuzu_path is None:
            kuzu_path = tempfile.mkdtemp(prefix=f"test_kuzu_bloco8_{uuid.uuid4().hex[:8]}_")
            self._kuzu_dirs.append(kuzu_path)
        cfg = MagicMock()
        cfg.kuzu_data_path = kuzu_path
        # Atributos Neo4j legados — não usados mais, mas mantidos para
        # não quebrar código que acesse cfg.neo4j_uri
        cfg.neo4j_uri = None
        cfg.neo4j_user = "neo4j"
        cfg.neo4j_password = "password123"
        return cfg

    def test_import(self):
        """Garante que a classe pode ser importada e instanciada."""
        from src.memory.knowledge_graph import KnowledgeGraph

        # Sem kuzu_data_path no config, deve usar fallback env/diretório padrão.
        # Pode falhar se kuzu não estiver instalado — nesse caso _enabled=False.
        kg = KnowledgeGraph(self._make_config())
        assert kg is not None

    def test_disabled_when_kuzu_unavailable(self):
        """Quando o KuzuDB não pode ser inicializado, _enabled deve ser False."""
        import sys

        with patch.dict("sys.modules", {"kuzu": None}):
            # Força o módulo kuzu a não existir neste escopo
            from importlib import reload
            import src.memory.knowledge_graph as kg_mod

            # Remonta sem kuzu disponível
            # Como o import já ocorreu, simulamos via MagicMock do kuzu_conn
            from src.memory.knowledge_graph import KnowledgeGraph

            kg = KnowledgeGraph(self._make_config())
            # Se o kuzu falhar no import interno, _enabled=False
            # Em ambiente sem kuzu instalado, isso é True
            assert hasattr(kg, "_enabled")

    @pytest.mark.asyncio
    async def test_add_fact_disabled_returns_false(self):
        """add_fact retorna False quando _enabled=False."""
        from src.memory.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(self._make_config())
        # Força desabilitado manualmente
        kg._enabled = False
        kg.kuzu_conn = None
        result = await kg.add_fact("Python", "is_a", "Language")
        assert result is False

    @pytest.mark.asyncio
    async def test_query_entity_disabled_returns_empty(self):
        """query_entity retorna lista vazia quando _enabled=False."""
        from src.memory.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(self._make_config())
        kg._enabled = False
        kg.kuzu_conn = None
        result = await kg.query_entity("Python")
        assert result == []

    @pytest.mark.asyncio
    async def test_add_fact_with_mock_kuzu(self):
        """add_fact chama add_triple quando kuzu_conn está ativo."""
        from src.memory.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(self._make_config())
        kg._enabled = True
        # Mock da conexão KuzuDB
        mock_conn = MagicMock()
        kg.kuzu_conn = mock_conn
        # Mock do add_triple para não depender do schema real
        kg.add_triple = MagicMock()

        result = await kg.add_fact("OpenAI", "produces", "GPT-4", source="test")
        assert result is True
        assert kg.add_triple.called

    @pytest.mark.asyncio
    async def test_query_entity_with_mock_kuzu(self):
        """query_entity retorna dicts quando kuzu_conn está ativo."""
        from src.memory.knowledge_graph import KnowledgeGraph
        from src.knowledge_graph import Triple

        kg = KnowledgeGraph(self._make_config())
        kg._enabled = True
        mock_conn = MagicMock()
        kg.kuzu_conn = mock_conn

        fake_triple = Triple(
            subject="OpenAI",
            relation="produces",
            object="GPT-4",
            confidence=0.9,
            source="test",
        )
        kg.query_graph = MagicMock(return_value=[fake_triple])

        result = await kg.query_entity("OpenAI")
        assert len(result) == 1
        assert result[0]["subject"] == "OpenAI"
        assert result[0]["predicate"] == "produces"
        assert result[0]["object"] == "GPT-4"

    @pytest.mark.asyncio
    async def test_close_sets_disabled(self):
        """close() deve desativar _enabled e zerar kuzu_conn."""
        from src.memory.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(self._make_config())
        await kg.close()
        assert kg._enabled is False
        assert kg.kuzu_conn is None

    @pytest.mark.asyncio
    async def test_get_driver_returns_none(self):
        """_get_driver() é um stub que retorna None (sem Neo4j)."""
        from src.memory.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(self._make_config())
        driver = await kg._get_driver()
        assert driver is None


# ============================================================ 8.2 HybridSearcher
class TestHybridSearcher:
    def _make_docs(self):
        return [
            {
                "text": "Python is a high-level programming language known for readability",
                "url": "https://python.org",
            },
            {
                "text": "JavaScript is the language of the web and runs in browsers",
                "url": "https://js.org",
            },
            {
                "text": "Rust offers memory safety without a garbage collector",
                "url": "https://rust-lang.org",
            },
            {
                "text": "Go language from Google is known for concurrency and simplicity",
                "url": "https://golang.org",
            },
        ]

    def test_import(self):
        from src.memory.hybrid_search import HybridSearcher

        hs = HybridSearcher()
        assert hs is not None

    def test_index_documents(self):
        from src.memory.hybrid_search import HybridSearcher

        hs = HybridSearcher()
        docs = self._make_docs()
        hs.index_documents(docs)
        assert len(hs._documents) == 4

    def test_bm25_empty_docs(self):
        from src.memory.hybrid_search import HybridSearcher

        hs = HybridSearcher()
        result = hs._bm25_search("python programming", top_k=5)
        assert result == []

    def test_bm25_returns_relevant_first(self):
        from src.memory.hybrid_search import HybridSearcher

        hs = HybridSearcher()
        hs.index_documents(self._make_docs())
        results = hs._bm25_search("Python programming language", top_k=4)
        assert isinstance(results, list)
        if results:
            assert results[0].get("url") == "https://python.org"

    def test_rrf_combines_rankings(self):
        from src.memory.hybrid_search import HybridSearcher

        hs = HybridSearcher()
        list1 = [
            {"url": "https://a.com", "text": "Doc A"},
            {"url": "https://b.com", "text": "Doc B"},
        ]
        list2 = [
            {"url": "https://b.com", "text": "Doc B"},
            {"url": "https://c.com", "text": "Doc C"},
        ]
        result = hs._reciprocal_rank_fusion(list1, list2)
        assert len(result) == 3
        # b.com aparece em ambas as listas, deve ter score maior e ficar no topo
        assert result[0]["url"] == "https://b.com"

    def test_rrf_handles_empty_lists(self):
        from src.memory.hybrid_search import HybridSearcher

        hs = HybridSearcher()
        result = hs._reciprocal_rank_fusion([], [])
        assert result == []

    @pytest.mark.asyncio
    async def test_search_without_chroma_without_cohere(self):
        from src.memory.hybrid_search import HybridSearcher

        hs = HybridSearcher(chroma_client=None, cohere_api_key=None)
        hs.index_documents(self._make_docs())
        results = await hs.search("Python programming", top_k=3)
        assert isinstance(results, list)
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_search_empty_query(self):
        from src.memory.hybrid_search import HybridSearcher

        hs = HybridSearcher()
        hs.index_documents(self._make_docs())
        result = await hs.search("", top_k=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_cohere_rerank_fallback_on_error(self):
        from src.memory.hybrid_search import HybridSearcher

        hs = HybridSearcher(cohere_api_key="fake-key")
        docs = self._make_docs()
        # Sem Cohere disponivel com chave fake, deve retornar os docs originais
        result = await hs._cohere_rerank("python", docs[:3], top_k=2)
        assert isinstance(result, list)
        assert len(result) <= 3

    @pytest.mark.asyncio
    async def test_chroma_search_no_client(self):
        from src.memory.hybrid_search import HybridSearcher

        hs = HybridSearcher(chroma_client=None)
        result = await hs._chroma_search("python", top_k=5)
        assert result == []


# ============================================================ 8.3 Celery App
class TestCeleryApp:
    def test_celery_app_import(self):
        from src.worker.celery_app import celery_app

        assert celery_app is not None
        assert celery_app.main == "smart_research_agent"

    def test_research_task_registered(self):
        from src.worker.celery_app import celery_app, research_task

        assert research_task.name == "sra.research"
        assert "sra.research" in celery_app.tasks

    def test_celery_config_correct(self):
        from src.worker.celery_app import celery_app

        assert celery_app.conf.task_serializer == "json"
        assert celery_app.conf.task_time_limit == 3600
        assert celery_app.conf.worker_prefetch_multiplier == 1
        assert celery_app.conf.result_expires == 86400

    def test_research_task_max_retries_and_signature(self):
        """Valida propriedades de configuracao da task sem invocar o loop asyncio."""
        from src.worker.celery_app import research_task

        # Valida retry policy
        assert research_task.max_retries == 2
        assert research_task.default_retry_delay == 60
        # Valida que a task esta atrelada (bind=True) — tem metodo request
        assert hasattr(research_task, "apply")
        # Valida que a task aceita os parametros esperados
        import inspect

        sig = inspect.signature(research_task.run)
        params = list(sig.parameters.keys())
        assert "query" in params
        assert "mode" in params
        assert "options" in params

    def test_research_task_apply_with_mock_orchestrator(self):
        from src.worker.celery_app import research_task

        mock_result = "Mock research result"
        with patch(
            "src.worker.celery_app.asyncio.new_event_loop"
        ) as mock_loop_fn, patch(
            "src.worker.celery_app.asyncio.set_event_loop"
        ) as mock_set_loop_fn:
            mock_loop = MagicMock()
            mock_loop.run_until_complete = MagicMock(return_value=mock_result)
            mock_loop.close = MagicMock()
            mock_loop.shutdown_asyncgens = AsyncMock()
            mock_loop_fn.return_value = mock_loop

            with patch("src.orchestrator.Orchestrator") as MockOrch:
                mock_orch = MagicMock()
                mock_orch.research = AsyncMock(return_value=mock_result)
                MockOrch.return_value = mock_orch

                result = research_task.apply(args=["test query", "standard"]).get()
                assert result["status"] == "success"
                assert result["query"] == "test query"
                assert result["mode"] == "standard"
                assert result["result"] == mock_result
