import pytest
from src.confidence_scorer import ConfidenceScorer
from src.types import SearchResult


def _make_result(
    url: str = "https://example.com",
    title: str = "Test Result",
    description: str = "word " * 100,
    source: str = "web",
    metrics: dict | None = None,
) -> SearchResult:
    return SearchResult(
        source=source,
        title=title,
        url=url,
        description=description,
        metrics=metrics or {},
    )


@pytest.fixture
def scorer() -> ConfidenceScorer:
    return ConfidenceScorer()


# ─── score_result: trusted domain bonus ──────────────────────────────────────

@pytest.mark.asyncio
async def test_trusted_domain_github_gets_bonus(scorer):
    result = _make_result(url="https://github.com/owner/repo", description="word " * 300)
    r = await scorer.score_result(result)
    assert r.confidence_score >= 0.6


@pytest.mark.asyncio
async def test_trusted_domain_arxiv_gets_bonus(scorer):
    result = _make_result(url="https://arxiv.org/abs/1234.5678", description="word " * 300)
    r = await scorer.score_result(result)
    assert r.confidence_score >= 0.6


# ─── score_result: content length penalties/bonuses ─────────────────────────

@pytest.mark.asyncio
async def test_rich_content_over_300_words_gets_bonus(scorer):
    result = _make_result(description="word " * 300, metrics={"score": 80})
    r = await scorer.score_result(result)
    assert r.confidence_score >= 0.6


@pytest.mark.asyncio
async def test_thin_content_under_50_words_penalized(scorer):
    result = _make_result(description="only a few words here")
    r = await scorer.score_result(result)
    assert r.confidence_score <= 0.3
    assert "content_too_short" in r.hallucination_flags


# ─── score_result: untrusted domain penalty ──────────────────────────────────

@pytest.mark.asyncio
async def test_untrusted_domain_penalized(scorer):
    result = _make_result(url="https://buzzfeed.com/article", description="word " * 50)
    r = await scorer.score_result(result)
    assert "untrusted_domain" in r.hallucination_flags


# ─── score_result: clickbait title ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_clickbait_title_flagged(scorer):
    result = _make_result(title="You won't believe this amazing hack!", description="word " * 100)
    r = await scorer.score_result(result)
    assert "clickbait_title" in r.hallucination_flags


@pytest.mark.asyncio
async def test_clean_title_no_clickbait_flag(scorer):
    result = _make_result(title="Comparing Python ORMs in 2026", description="word " * 100)
    r = await scorer.score_result(result)
    assert "clickbait_title" not in r.hallucination_flags


# ─── score_result: absolute claims ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_absolute_claim_in_title_penalized(scorer):
    result = _make_result(title="The best Python framework definitivo", description="word " * 100)
    r = await scorer.score_result(result)
    assert "absolute_claim_detected" in r.hallucination_flags


# ─── score_result: date detection ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_date_in_content_adds_bonus(scorer):
    result_with_date = _make_result(description="Released on 2026-01 this framework improved. " * 20)
    result_without_date = _make_result(description="word " * 40)
    r_with = await scorer.score_result(result_with_date)
    r_without = await scorer.score_result(result_without_date)
    assert r_with.confidence_score > r_without.confidence_score


# ─── score_result: citations ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_citations_extracted_from_content(scorer):
    desc = "See https://github.com/foo/bar and https://arxiv.org/abs/123 for details. " * 5
    result = _make_result(description=desc)
    r = await scorer.score_result(result)
    assert len(r.citations) >= 2
    assert any("github.com" in c for c in r.citations)


# ─── score_result: repetition detection ──────────────────────────────────────

@pytest.mark.asyncio
async def test_repetitive_content_flagged(scorer):
    desc = "buy now buy now buy now buy now buy now buy now buy now buy now"
    result = _make_result(description=desc * 5)
    r = await scorer.score_result(result)
    assert "repetitive_content" in r.hallucination_flags


# ─── score_result: evidence_quality classification ───────────────────────────

@pytest.mark.asyncio
async def test_evidence_quality_verified_for_high_score(scorer):
    result = _make_result(
        url="https://github.com/owner/repo",
        description="word " * 400,
        metrics={"score": 80},
    )
    r = await scorer.score_result(result)
    assert r.evidence_quality == "verified"


@pytest.mark.asyncio
async def test_evidence_quality_unknown_for_low_score(scorer):
    result = _make_result(description="short")
    r = await scorer.score_result(result)
    assert r.evidence_quality in ("unknown", "inferred")


# ─── score_result: score clamped to [0.0, 1.0] ───────────────────────────────

@pytest.mark.asyncio
async def test_score_never_exceeds_1(scorer):
    result = _make_result(
        url="https://github.com/x/y",
        title="A good neutral title",
        description="word " * 500,
        metrics={"score": 95},
    )
    r = await scorer.score_result(result)
    assert 0.0 <= r.confidence_score <= 1.0


@pytest.mark.asyncio
async def test_score_never_below_0(scorer):
    result = _make_result(
        url="https://buzzfeed.com/x",
        title="You won't believe the best and only definitivo hack!",
        description="short",
    )
    r = await scorer.score_result(result)
    assert r.confidence_score >= 0.0


# ─── score_batch ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_batch_returns_all_results(scorer):
    results = [_make_result(description="word " * 100) for _ in range(5)]
    scored = await scorer.score_batch(results, cross_validate=False)
    assert len(scored) == 5
    assert all(r.confidence_score > 0 for r in scored)


@pytest.mark.asyncio
async def test_score_batch_cross_validate_detects_contradictions(scorer):
    r1 = _make_result(
        url="https://site1.com",
        title="FastAPI is fast and stable",
        description="FastAPI is fast reliable stable recommended for production. " * 10,
    )
    r2 = _make_result(
        url="https://site2.com",
        title="FastAPI is slow and broken",
        description="FastAPI is slow broken deprecated avoid in production. " * 10,
    )
    scored = await scorer.score_batch([r1, r2], cross_validate=True)
    contradicted = [r for r in scored if r.contradictions]
    assert len(contradicted) > 0


@pytest.mark.asyncio
async def test_score_batch_no_cross_validate_skips_contradiction_check(scorer):
    results = [_make_result(description="word " * 50) for _ in range(3)]
    scored = await scorer.score_batch(results, cross_validate=False)
    assert all(r.contradictions == [] for r in scored)


# ─── _detect_contradictions ──────────────────────────────────────────────────

def test_detect_contradictions_finds_opposing_claims(scorer):
    r1 = _make_result(
        url="https://a.com",
        title="Django ORM performance",
        description="Django ORM is fast reliable stable recommended. " * 5,
    )
    r2 = _make_result(
        url="https://b.com",
        title="Django ORM performance issues",
        description="Django ORM is slow broken deprecated avoid. " * 5,
    )
    result = scorer._detect_contradictions([r1, r2])
    assert "https://a.com" in result or "https://b.com" in result


def test_detect_contradictions_ignores_unrelated(scorer):
    r1 = _make_result(url="https://a.com", title="Python tips", description="Python is great. " * 5)
    r2 = _make_result(url="https://b.com", title="Rust tips", description="Rust is memory safe. " * 5)
    result = scorer._detect_contradictions([r1, r2])
    assert result == {}


# ─── _extract_domain ────────────────────────────────────────────────────────

def test_extract_domain_standard_url(scorer):
    assert scorer._extract_domain("https://github.com/owner/repo") == "github.com"


def test_extract_domain_with_www(scorer):
    assert scorer._extract_domain("https://www.reddit.com/r/python") == "reddit.com"


def test_extract_domain_empty_url(scorer):
    assert scorer._extract_domain("") == ""


# ─── _has_repetition ────────────────────────────────────────────────────────

def test_has_repetition_detects_spam(scorer):
    text = "buy now buy now buy now buy now buy now buy now buy now buy now"
    assert scorer._has_repetition(text * 3) is True


def test_has_repetition_normal_text(scorer):
    text = "Python is a versatile programming language used for web development and data science."
    assert scorer._has_repetition(text) is False


# ─── _classify_evidence_quality ─────────────────────────────────────────────

def test_classify_evidence_quality_levels(scorer):
    assert scorer._classify_evidence_quality(0.80) == "verified"
    assert scorer._classify_evidence_quality(0.60) == "cited"
    assert scorer._classify_evidence_quality(0.40) == "inferred"
    assert scorer._classify_evidence_quality(0.20) == "unknown"
