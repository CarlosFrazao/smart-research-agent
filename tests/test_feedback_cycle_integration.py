"""
Teste de integração do ciclo completo de feedback.

Valida que a cadeia: SearchStage gera result_id → FeedbackStore.record() →
FeedbackRanker.apply() → combined_score alterado funciona de ponta a ponta.

Este teste teria pego o bug dos IDs incompatíveis imediatamente.
"""
import pytest
import tempfile
from datetime import datetime

from src.types import SearchResult, RankedResult, SynthesizedResult, generate_result_id
from src.feedback_store import FeedbackStore
from src.feedback_ranker import FeedbackRanker


class TestFeedbackCycleIntegration:
    """Testes de integração do ciclo completo de feedback."""

    def test_result_id_is_deterministic(self):
        """O mesmo (source, url) sempre gera o mesmo result_id."""
        id1 = generate_result_id("github", "https://github.com/user/repo")
        id2 = generate_result_id("github", "https://github.com/user/repo")
        assert id1 == id2

    def test_result_id_differs_by_source_and_url(self):
        """IDs diferentes para URLs e fontes diferentes."""
        id_github = generate_result_id("github", "https://github.com/user/repo")
        id_arxiv = generate_result_id("arxiv", "https://arxiv.org/abs/1234")
        id_diff_url = generate_result_id("github", "https://github.com/user/other")
        assert id_github != id_arxiv
        assert id_github != id_diff_url

    def test_search_result_has_result_id_field(self):
        """SearchResult tem campo result_id populável."""
        result = SearchResult(
            source="github",
            title="Test Repo",
            url="https://github.com/user/repo",
            description="A test repo"
        )
        result.result_id = generate_result_id("github", result.url)
        assert result.result_id
        assert len(result.result_id) == 12

    def test_synthesized_result_inherits_result_id(self):
        """SynthesizedResult herda result_id do SearchResult."""
        # Cria SearchResult com result_id
        search_result = RankedResult(
            source="github",
            title="Test Repo",
            url="https://github.com/user/repo",
            description="A test repo",
            score=80.0,
        )
        search_result.result_id = generate_result_id("github", search_result.url)

        # Cria SynthesizedResult simulando o merge do cluster
        synthesized = SynthesizedResult(
            entity="test",
            title=search_result.title,
            description=search_result.description,
            sources=[search_result.source],
            urls=[search_result.url],
            combined_score=80.0,
            metrics={},
            highlights=[],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
            result_id=search_result.result_id,
        )
        assert synthesized.result_id == search_result.result_id

    def test_feedback_affects_ranker_score(self, tmp_path):
        """
        CRÍTICO: Após record() com signal='useful', FeedbackRanker.apply()
        deve produzir um combined_score diferente para aquele resultado.
        """
        # Setup: FeedbackStore com path temporário
        store = FeedbackStore(store_path=tmp_path / "feedback.jsonl")
        ranker = FeedbackRanker(store)

        # Cria um SynthesizedResult com result_id canônico
        result_id = generate_result_id("github", "https://github.com/user/repo")
        result = SynthesizedResult(
            entity="test",
            title="Test Project",
            description="A test project",
            sources=["github"],
            urls=["https://github.com/user/repo"],
            combined_score=50.0,
            metrics={},
            highlights=[],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
            result_id=result_id,
        )

        # Registrar feedback positivo (store.record é síncrono)
        store.record(
            result_id=result_id,
            signal="useful",
            query="python async patterns",
            source_name="github",
        )

        # Verificar que o ranker ajusta o score
        results_after = ranker.apply([result])

        # O score deve ter mudado positivamente (useful = +1.5 * 5 = +7.5 → 57.5)
        assert results_after[0].combined_score != 50.0, (
            "FeedbackRanker não alterou o score — verifique se result_id está sendo "
            "comparado corretamente entre FeedbackStore e FeedbackRanker"
        )
        assert results_after[0].combined_score > 50.0, "Score deve aumentar com feedback útil"

    def test_negative_feedback_reduces_score(self, tmp_path):
        """Feedback negativo reduz o combined_score."""
        store = FeedbackStore(store_path=tmp_path / "feedback.jsonl")
        ranker = FeedbackRanker(store)

        result_id = generate_result_id("github", "https://github.com/user/bad-repo")
        result = SynthesizedResult(
            entity="bad",
            title="Bad Project",
            description="A bad project",
            sources=["github"],
            urls=["https://github.com/user/bad-repo"],
            combined_score=70.0,
            metrics={},
            highlights=[],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
            result_id=result_id,
        )

        # Registrar feedback negativo
        store.record(
            result_id=result_id,
            signal="irrelevant",
            query="bad search",
            source_name="github",
        )

        results_after = ranker.apply([result])

        # irrelevant = -1.5 * 5 = -7.5 → score deve cair
        assert results_after[0].combined_score < 70.0, "Score deve diminuir com feedback negativo"

    def test_score_clamped_to_bounds(self, tmp_path):
        """Score nunca passa de 100 ou cai abaixo de 0."""
        store = FeedbackStore(store_path=tmp_path / "feedback.jsonl")
        ranker = FeedbackRanker(store)

        result_id = generate_result_id("github", "https://github.com/user/perfect-repo")
        result = SynthesizedResult(
            entity="perfect",
            title="Perfect Project",
            description="A perfect project",
            sources=["github"],
            urls=["https://github.com/user/perfect-repo"],
            combined_score=98.0,
            metrics={},
            highlights=[],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
            result_id=result_id,
        )

        # Muitos feedbacks positivos
        for _ in range(20):
            store.record(
                result_id=result_id,
                signal="bookmark",
                query="perfect search",
                source_name="github",
            )

        results_after = ranker.apply([result])

        # Deve estar clamped no máximo de 100
        assert results_after[0].combined_score <= 100.0

    def test_ranker_reorders_by_adjusted_score(self, tmp_path):
        """Ranker reordena resultados baseado no score ajustado."""
        store = FeedbackStore(store_path=tmp_path / "feedback.jsonl")
        ranker = FeedbackRanker(store)

        # Resultado A: score alto, mas feedback negativo extremo (capped)
        result_id_a = generate_result_id("github", "https://github.com/user/high-score")
        result_a = SynthesizedResult(
            entity="high",
            title="High Score",
            description="High score project",
            sources=["github"],
            urls=["https://github.com/user/high-score"],
            combined_score=60.0,
            metrics={},
            highlights=[],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
            result_id=result_id_a,
        )

        # Resultado B: score baixo, mas feedback positivo extremo (capped)
        result_id_b = generate_result_id("github", "https://github.com/user/low-score")
        result_b = SynthesizedResult(
            entity="low",
            title="Low Score",
            description="Low score project",
            sources=["github"],
            urls=["https://github.com/user/low-score"],
            combined_score=55.0,
            metrics={},
            highlights=[],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
            result_id=result_id_b,
        )

        # A recebe penalidade máxima (-15), B recebe bônus máximo (+15)
        for _ in range(10):
            store.record(result_id=result_id_a, signal="irrelevant", query="q", source_name="github")
            store.record(result_id=result_id_b, signal="bookmark", query="q", source_name="github")

        results = ranker.apply([result_a, result_b])

        # A: 60 - 15 = 45.0, B: 55 + 15 = 70.0 → B deve ultrapassar A
        assert results[0].title == "Low Score"
        assert results[1].title == "High Score"

    def test_original_object_not_mutated(self, tmp_path):
        """O objeto original não é mutado — retorna cópia."""
        store = FeedbackStore(store_path=tmp_path / "feedback.jsonl")
        ranker = FeedbackRanker(store)

        result_id = generate_result_id("github", "https://github.com/user/immutable")
        result = SynthesizedResult(
            entity="immutable",
            title="Immutable",
            description="Test",
            sources=["github"],
            urls=["https://github.com/user/immutable"],
            combined_score=50.0,
            metrics={},
            highlights=[],
            first_seen=datetime.now(),
            last_seen=datetime.now(),
            result_id=result_id,
        )

        store.record(result_id=result_id, signal="useful", query="q", source_name="github")

        ranker.apply([result])

        # Original deve permanecer inalterado
        assert result.combined_score == 50.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
