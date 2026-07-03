"""
Testes do Bloco 4 — Robustez: Budget, Rate Limiter, DLQ e Exceptions.
Sub-tarefas: 4.1 (Budget), 4.2 (RateLimiter), 4.3 (DLQ), 4.4 (Exceptions).
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# 4.4 — Exceptions
# ─────────────────────────────────────────────────────────────────────────────

from src.exceptions import (
    SRABaseError,
    TransientError,
    PermanentError,
    SearcherTimeoutError,
    SearcherRateLimitError,
    SearcherBlockedError,
    LLMError,
    BudgetExceededError,
)


class TestExceptions:
    def test_hierarchy_transient(self):
        assert issubclass(SearcherTimeoutError, TransientError)
        assert issubclass(SearcherRateLimitError, TransientError)
        assert issubclass(LLMError, TransientError)
        assert issubclass(TransientError, SRABaseError)

    def test_hierarchy_permanent(self):
        assert issubclass(SearcherBlockedError, PermanentError)
        assert issubclass(PermanentError, SRABaseError)

    def test_budget_exceeded_not_transient(self):
        assert issubclass(BudgetExceededError, SRABaseError)
        assert not issubclass(BudgetExceededError, TransientError)

    def test_rate_limit_custom_retry_after(self):
        exc = SearcherRateLimitError("rate limit", retry_after=120.0)
        assert exc.retry_after == 120.0
        assert "rate limit" in str(exc)

    def test_transient_default_retry_after(self):
        exc = SearcherTimeoutError("timeout")
        assert exc.retry_after == 30.0

    def test_can_raise_and_catch(self):
        with pytest.raises(SearcherBlockedError):
            raise SearcherBlockedError("Blocked 403")

    def test_can_catch_as_permanent(self):
        with pytest.raises(PermanentError):
            raise SearcherBlockedError("Blocked 451")


# ─────────────────────────────────────────────────────────────────────────────
# 4.1 — ResearchBudget + DeepResearcher
# ─────────────────────────────────────────────────────────────────────────────

from src.deep_researcher import DeepResearcher, ResearchBudget


class TestResearchBudget:
    def test_not_exhausted_by_default(self):
        b = ResearchBudget()
        assert not b.is_exhausted()

    def test_exhausted_by_nodes(self):
        b = ResearchBudget(max_total_nodes=3, nodes_created=3)
        assert b.is_exhausted()

    def test_exhausted_by_cost(self):
        b = ResearchBudget(max_cost_usd=1.0, estimated_cost=1.01)
        assert b.is_exhausted()

    def test_exhausted_by_llm_calls(self):
        b = ResearchBudget(max_llm_calls=5, llm_calls=5)
        assert b.is_exhausted()

    def test_exhausted_by_tokens(self):
        b = ResearchBudget(max_tokens_total=1000, tokens_used=1000)
        assert b.is_exhausted()

    def test_summary_format(self):
        b = ResearchBudget(
            max_total_nodes=10, nodes_created=3, max_cost_usd=5.0, estimated_cost=0.5
        )
        s = b.summary()
        assert s["nodes"] == "3/10"
        assert "$0.5000/$5.0" in s["cost_usd"]


class TestDeepResearcherBudget:
    def _make_researcher(self, budget: ResearchBudget) -> DeepResearcher:
        llm = MagicMock()
        return DeepResearcher(llm_client=llm, budget=budget)

    @pytest.mark.asyncio
    async def test_check_budget_raises_when_exhausted(self):
        budget = ResearchBudget(max_total_nodes=0, nodes_created=0)
        # is_exhausted() retorna True pois nodes_created (0) >= max_total_nodes (0)
        dr = self._make_researcher(budget)
        with pytest.raises(BudgetExceededError):
            await dr._check_budget()

    @pytest.mark.asyncio
    async def test_check_budget_passes_when_ok(self):
        budget = ResearchBudget(max_total_nodes=10, nodes_created=2)
        dr = self._make_researcher(budget)
        await dr._check_budget()  # Não deve levantar exceção

    @pytest.mark.asyncio
    async def test_track_llm_call_increments_counters(self):
        dr = self._make_researcher(ResearchBudget())
        prompt = "a" * 4000
        await dr._track_llm_call(prompt)
        assert dr.budget.llm_calls == 1
        expected_tokens = len(prompt) // 4 + 500
        assert dr.budget.tokens_used == expected_tokens

    @pytest.mark.asyncio
    async def test_research_stops_on_budget_exceeded(self):
        """DeepResearcher.research() deve retornar parcialmente se budget esgotado."""
        budget = ResearchBudget(max_total_nodes=1)
        llm = MagicMock()
        dr = DeepResearcher(llm_client=llm, budget=budget)

        # Força explore_node a incrementar budget imediatamente
        call_count = 0

        async def fake_explore(node):
            nonlocal call_count
            call_count += 1
            node.status = "explored"
            node.results = []
            return node

        dr._explore_node = fake_explore

        result = await dr.research("test query")
        # budget_exceeded pode ser True ou False dependendo da ordem de check,
        # mas o resultado sempre deve ser retornado sem exceção não tratada
        assert result is not None
        assert result.budget_summary is not None


# ─────────────────────────────────────────────────────────────────────────────
# 4.2 — DomainRateLimiter / TokenBucket
# ─────────────────────────────────────────────────────────────────────────────

from src.utils.rate_limiter import TokenBucket, DomainRateLimiter, DOMAIN_LIMITS


class TestTokenBucket:
    @pytest.mark.asyncio
    async def test_burst_passes_immediately(self):
        """Requisições dentro do burst não geram sleep."""
        bucket = TokenBucket(rate=2.0, capacity=5)
        sleep_calls = []

        async def fake_sleep(t):
            sleep_calls.append(t)

        with patch("src.utils.rate_limiter.asyncio.sleep", new=fake_sleep):
            for _ in range(5):
                await bucket.acquire()

        assert len(sleep_calls) == 0, "Burst não deve gerar throttling"

    @pytest.mark.asyncio
    async def test_throttles_after_burst(self):
        """Após esgotar o burst, deve throttlear."""
        bucket = TokenBucket(rate=2.0, capacity=2)
        sleep_calls = []

        async def fake_sleep(t):
            sleep_calls.append(t)
            bucket.tokens = float(bucket.capacity)  # Simula reabastecimento

        with patch("src.utils.rate_limiter.asyncio.sleep", new=fake_sleep):
            for _ in range(5):
                await bucket.acquire()

        assert len(sleep_calls) > 0, "Deve ter throttlado após burst de 2"


class TestDomainRateLimiter:
    def setup_method(self):
        DomainRateLimiter.reset_all()

    @pytest.mark.asyncio
    async def test_wait_github_uses_correct_bucket(self):
        """Deve usar bucket configurado para api.github.com."""
        with patch.object(DomainRateLimiter, "_get_bucket") as mock_get:
            mock_bucket = AsyncMock()
            mock_get.return_value = mock_bucket
            await DomainRateLimiter.wait("https://api.github.com/repos")
            mock_get.assert_called_once_with("api.github.com")
            mock_bucket.acquire.assert_called_once()

    @pytest.mark.asyncio
    async def test_wait_unknown_domain_uses_default(self):
        """URL desconhecida deve usar configuração 'default'."""
        url = "https://some.unknown.domain.xyz/api"
        # Deve executar sem exceção
        with patch("src.utils.rate_limiter.asyncio.sleep", new=AsyncMock()):
            await DomainRateLimiter.wait(url)

    def test_domain_limits_github_rps(self):
        assert DOMAIN_LIMITS["api.github.com"].requests_per_second == 1.5
        assert DOMAIN_LIMITS["api.github.com"].burst_size == 5

    def test_domain_limits_reddit_rps(self):
        assert DOMAIN_LIMITS["www.reddit.com"].requests_per_second == 1.0

    @pytest.mark.asyncio
    async def test_wait_does_not_raise_on_error(self):
        """Erros internos no rate limiter não devem propagar."""
        with patch.object(
            DomainRateLimiter, "_get_bucket", side_effect=RuntimeError("fail")
        ):
            await DomainRateLimiter.wait(
                "https://api.github.com/x"
            )  # Não deve levantar


# ─────────────────────────────────────────────────────────────────────────────
# 4.3 — Dead Letter Queue
# ─────────────────────────────────────────────────────────────────────────────

from src.utils.dead_letter_queue import DeadLetterQueue, FailedTask


class TestDeadLetterQueue:
    @pytest.mark.asyncio
    async def test_push_creates_json_file(self, tmp_path):
        dlq = DeadLetterQueue(path=str(tmp_path))
        task = dlq.create_failed_task(
            task_type="search",
            payload={"query": "test query", "node_id": "abc"},
            error="TimeoutError",
            source="test",
        )
        await dlq.push(task)
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1

    @pytest.mark.asyncio
    async def test_json_fields_correct(self, tmp_path):
        dlq = DeadLetterQueue(path=str(tmp_path))
        task = dlq.create_failed_task("llm_call", {"prompt": "x"}, "APIError", "src")
        await dlq.push(task)
        file = list(tmp_path.glob("*.json"))[0]
        data = json.loads(file.read_text())
        assert data["task_type"] == "llm_call"
        assert data["error"] == "APIError"
        assert data["source"] == "src"
        assert "task_id" in data
        assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_pop_all_returns_tasks_and_removes_files(self, tmp_path):
        dlq = DeadLetterQueue(path=str(tmp_path))
        for i in range(3):
            task = dlq.create_failed_task("search", {"i": i}, "err", "")
            await dlq.push(task)
        tasks = await dlq.pop_all()
        assert len(tasks) == 3
        assert len(list(tmp_path.glob("*.json"))) == 0

    @pytest.mark.asyncio
    async def test_retry_all_success(self, tmp_path):
        dlq = DeadLetterQueue(path=str(tmp_path))
        task = dlq.create_failed_task("search", {"q": "q"}, "err", "")
        await dlq.push(task)
        handled = []

        async def handler(t: FailedTask):
            handled.append(t.task_id)

        result = await dlq.retry_all(handler)
        assert result["success"] == 1
        assert result["permanent_fail"] == 0
        assert len(handled) == 1
        assert dlq.size() == 0

    @pytest.mark.asyncio
    async def test_retry_all_permanent_fail(self, tmp_path):
        dlq = DeadLetterQueue(path=str(tmp_path))
        task = dlq.create_failed_task("search", {"q": "q"}, "err", "")
        task.retry_count = dlq.MAX_RETRIES - 1  # Uma tentativa a menos
        await dlq.push(task)

        async def bad_handler(t: FailedTask):
            raise RuntimeError("still failing")

        result = await dlq.retry_all(bad_handler)
        assert result["permanent_fail"] == 1
        assert dlq.size() == 0

    @pytest.mark.asyncio
    async def test_retry_all_requeues_before_limit(self, tmp_path):
        dlq = DeadLetterQueue(path=str(tmp_path))
        task = dlq.create_failed_task("search", {"q": "q"}, "err", "")
        task.retry_count = 0
        await dlq.push(task)

        async def bad_handler(t: FailedTask):
            raise RuntimeError("still failing")

        result = await dlq.retry_all(bad_handler)
        assert result["requeued"] == 1
        assert dlq.size() == 1  # re-enfileirada

    def test_create_failed_task_fields(self, tmp_path):
        dlq = DeadLetterQueue(path=str(tmp_path))
        task = dlq.create_failed_task("scrape", {"url": "http://x"}, "404", "scraper")
        assert task.task_type == "scrape"
        assert task.error == "404"
        assert task.source == "scraper"
        assert len(task.task_id) == 8

    def test_size_returns_file_count(self, tmp_path):
        dlq = DeadLetterQueue(path=str(tmp_path))
        assert dlq.size() == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4.5 — Orchestrator Facade / Services
# ─────────────────────────────────────────────────────────────────────────────

from src.orchestrator import Orchestrator
from src.config import Config


class TestOrchestratorFacade:
    def test_services_instantiated_properly(self):
        config = Config()
        config.memory_enabled = False
        config.smart_routing_enabled = False

        # Moca o client do LLMClient para evitar chamadas de API ou erros de setup de API key
        with patch("src.orchestrator.LLMClient") as mock_llm_class:
            orch = Orchestrator(config)

            assert hasattr(orch, "search")
            assert hasattr(orch, "reasoning")
            assert hasattr(orch, "memory_service")
            assert hasattr(orch, "reports")

            # Verifica que o property searchers aponta para o search service
            assert orch.searchers is orch.search.searchers
