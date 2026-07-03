import pytest


# ------------------------------------------------------------------ helpers
class MockSearcher:
    async def search(self, query: str):
        return [
            {"url": f"https://example.com/{query}/1", "title": "R1", "lang": "?"},
            {"url": f"https://example.com/{query}/2", "title": "R2", "lang": "?"},
        ]


class MockLLM:
    def __init__(self, name="mock"):
        self.model = name

    async def complete(self, prompt: str) -> str:
        if "Translate" in prompt:
            return "python assincrono"
        # For fact-check
        return "VERDICT: supported\nCONFIDENCE: 0.9\nREASONING: Evidence supports the claim."


class FailingLLM:
    model = "failing"

    async def complete(self, prompt: str) -> str:
        raise RuntimeError("LLM down")


# ============================================================ 7.1 Multilingual
class TestMultilingualSearcher:
    def test_import(self):
        from src.search.multilingual_searcher import MultilingualSearcher

        s = MultilingualSearcher(MockSearcher())
        assert s is not None

    @pytest.mark.asyncio
    async def test_search_single_lang(self):
        from src.search.multilingual_searcher import MultilingualSearcher

        s = MultilingualSearcher(MockSearcher())
        results = await s.search("LLMs", languages=["en"])
        assert isinstance(results, list)
        assert len(results) == 2
        assert all("url" in r for r in results)

    @pytest.mark.asyncio
    async def test_search_deduplicate_by_url(self):
        from src.search.multilingual_searcher import MultilingualSearcher

        class SameUrlSearcher:
            async def search(self, query: str):
                return [{"url": "https://example.com/same", "title": "Same"}]

        s = MultilingualSearcher(SameUrlSearcher())
        results = await s.search("query", languages=["en", "pt"])
        # Both langs return same URL => dedup => only 1 result
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_translation_with_llm(self):
        from src.search.multilingual_searcher import MultilingualSearcher

        s = MultilingualSearcher(MockSearcher(), llm_client=MockLLM())
        results = await s.search("python async", languages=["pt"])
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_searcher_failure_graceful(self):
        from src.search.multilingual_searcher import MultilingualSearcher

        class BadSearcher:
            async def search(self, query: str):
                raise RuntimeError("Network down")

        s = MultilingualSearcher(BadSearcher())
        results = await s.search("test", languages=["en", "pt"])
        assert results == []


# ============================================================ 7.2 OCRExtractor
class TestOCRExtractor:
    def test_import(self):
        from src.extractors.ocr_extractor import OCRExtractor

        ocr = OCRExtractor()
        assert ocr is not None

    def test_unavailable_without_tesseract(self):
        from src.extractors.ocr_extractor import OCRExtractor

        ocr = OCRExtractor()
        # If tesseract not installed, _check_available returns False
        # (may be True if installed, we just check it doesn't raise)
        result = ocr._check_available()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_extract_from_bytes_no_crash(self):
        from src.extractors.ocr_extractor import OCRExtractor

        ocr = OCRExtractor()
        # Even with blank bytes, should not crash
        result = await ocr.extract_from_bytes(b"not an image")
        assert result is None or isinstance(result, str)

    @pytest.mark.asyncio
    async def test_extract_from_file_missing_file(self):
        from src.extractors.ocr_extractor import OCRExtractor

        ocr = OCRExtractor()
        result = await ocr.extract_from_file("/nonexistent/path/image.png")
        assert result is None


# ============================================================ 7.3 VideoTranscriber
class TestVideoTranscriber:
    def test_import(self):
        from src.extractors.video_transcriber import VideoTranscriber

        vt = VideoTranscriber()
        assert vt is not None

    def test_get_model_gracious(self):
        from src.extractors.video_transcriber import VideoTranscriber

        vt = VideoTranscriber(model_name="base")
        # Should not raise; returns None if whisper not installed
        model = vt._get_model()
        assert model is None or hasattr(model, "transcribe")

    @pytest.mark.asyncio
    async def test_transcribe_file_missing_path(self):
        from src.extractors.video_transcriber import VideoTranscriber

        vt = VideoTranscriber()
        result = await vt.transcribe_file("/nonexistent/audio.mp3")
        assert result is None


# ============================================================ 7.4 PDFParser
class TestPDFParser:
    def test_import(self):
        from src.extractors.pdf_parser import PDFParser

        p = PDFParser()
        assert p is not None

    @pytest.mark.asyncio
    async def test_parse_file_nonexistent(self):
        from src.extractors.pdf_parser import PDFParser

        p = PDFParser()
        result = await p.parse_file("/nonexistent/file.pdf")
        assert "text" in result
        assert "tables" in result
        assert "references" in result

    @pytest.mark.asyncio
    async def test_parse_bytes_invalid(self):
        from src.extractors.pdf_parser import PDFParser

        p = PDFParser()
        result = await p.parse_bytes(b"not a pdf")
        assert isinstance(result, dict)
        assert "text" in result

    def test_extract_references_parses_section(self):
        from src.extractors.pdf_parser import PDFParser

        p = PDFParser()
        text = "Introduction\nSome text.\n\nReferences\n1. Smith et al. 2020. Title.\n2. Jones 2019. Another."
        refs = p._extract_references(text)
        assert isinstance(refs, list)


# ============================================================ 7.5 FirecrawlClient Agent Mode
class TestFirecrawlAgentMode:
    def test_agent_methods_exist(self):
        from src.clients.firecrawl_client import FirecrawlClient

        fc = FirecrawlClient(api_key="")
        assert hasattr(fc, "agent_search")
        assert hasattr(fc, "interact")
        assert hasattr(fc, "map_domain")
        assert hasattr(fc, "batch_scrape")
        assert hasattr(fc, "_post")

    @pytest.mark.asyncio
    async def test_agent_search_no_key_returns_empty(self):
        from src.clients.firecrawl_client import FirecrawlClient

        fc = FirecrawlClient(api_key="")
        result = await fc.agent_search("test task")
        assert result == {}

    @pytest.mark.asyncio
    async def test_interact_no_key_returns_empty(self):
        from src.clients.firecrawl_client import FirecrawlClient

        fc = FirecrawlClient(api_key="")
        result = await fc.interact("https://example.com", ["click button"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_map_domain_no_key_returns_empty(self):
        from src.clients.firecrawl_client import FirecrawlClient

        fc = FirecrawlClient(api_key="")
        result = await fc.map_domain("https://example.com")
        assert result == []

    @pytest.mark.asyncio
    async def test_batch_scrape_no_key_returns_empty(self):
        from src.clients.firecrawl_client import FirecrawlClient

        fc = FirecrawlClient(api_key="")
        result = await fc.batch_scrape(["https://example.com"])
        assert result == []

    @pytest.mark.asyncio
    async def test_batch_scrape_empty_urls(self):
        from src.clients.firecrawl_client import FirecrawlClient

        fc = FirecrawlClient(api_key="some-key")
        result = await fc.batch_scrape([])
        assert result == []


# ============================================================ 7.6 MultiLLMFactChecker
class TestMultiLLMFactChecker:
    def test_import(self):
        from src.reasoning.multi_llm_fact_checker import MultiLLMFactChecker

        checker = MultiLLMFactChecker([MockLLM()])
        assert checker is not None

    def test_requires_llm(self):
        from src.reasoning.multi_llm_fact_checker import MultiLLMFactChecker

        with pytest.raises(ValueError):
            MultiLLMFactChecker([])

    @pytest.mark.asyncio
    async def test_verify_claim_supported(self):
        from src.reasoning.multi_llm_fact_checker import (
            MultiLLMFactChecker,
            FactCheckResult,
        )

        checker = MultiLLMFactChecker([MockLLM("llm1"), MockLLM("llm2")])
        result = await checker.verify_claim(
            "Python is popular",
            evidence=["Python is the most popular language on Stack Overflow 2024."],
        )
        assert isinstance(result, FactCheckResult)
        assert result.verdict == "supported"
        assert result.confidence >= 0.8
        assert result.consensus is True

    @pytest.mark.asyncio
    async def test_verify_claim_all_llm_fail(self):
        from src.reasoning.multi_llm_fact_checker import MultiLLMFactChecker

        checker = MultiLLMFactChecker([FailingLLM(), FailingLLM()])
        result = await checker.verify_claim("some claim", evidence=["evidence"])
        assert result.verdict == "uncertain"
        assert result.confidence == 0.0
        assert result.consensus is False

    @pytest.mark.asyncio
    async def test_verify_batch_multiple_claims(self):
        from src.reasoning.multi_llm_fact_checker import MultiLLMFactChecker

        checker = MultiLLMFactChecker([MockLLM()])
        results = await checker.verify_batch(
            ["claim 1", "claim 2", "claim 3"], evidence=["evidence a", "evidence b"]
        )
        assert len(results) == 3
        for r in results:
            assert r.verdict in ("supported", "refuted", "uncertain")

    def test_parse_response_unknown_verdict(self):
        from src.reasoning.multi_llm_fact_checker import MultiLLMFactChecker

        checker = MultiLLMFactChecker([MockLLM()])
        parsed = checker._parse_response(
            "VERDICT: maybe\nCONFIDENCE: 0.7\nREASONING: unclear", "llm"
        )
        assert parsed["verdict"] == "uncertain"
        assert parsed["confidence"] == 0.7

    def test_calculate_consensus_majority(self):
        from src.reasoning.multi_llm_fact_checker import MultiLLMFactChecker

        checker = MultiLLMFactChecker([MockLLM()])
        verdicts = [
            {"verdict": "supported", "confidence": 0.9},
            {"verdict": "supported", "confidence": 0.8},
            {"verdict": "refuted", "confidence": 0.6},
        ]
        dominant, conf, consensus = checker._calculate_consensus(verdicts)
        assert dominant == "supported"
        assert consensus is True
