import pytest
from src.evidence_graph import EvidenceGraph, Claim, ClaimRelation
from src.types import SearchResult


def _make_result(url: str, source: str, title: str, desc: str, confidence: float = 0.8) -> SearchResult:
    r = SearchResult(source=source, title=title, url=url, description=desc)
    r.confidence_score = confidence
    return r


# ── Bloco 1: Extração de Claims ───────────────────────────────────────────────

def test_extract_claims_from_valid_results():
    graph = EvidenceGraph()
    results = [
        _make_result(
            url="http://a.com", source="sourceA",
            title="AI adoption rises",
            desc="Studies show that AI adoption in enterprises increased by 45 percent in 2024, "
                 "driven by productivity gains and cost reduction strategies across all sectors.",
        ),
    ]
    graph.build_from_results(results)
    assert len(graph.claims) >= 1
    assert all(isinstance(c, Claim) for c in graph.claims)


def test_extract_ignores_short_sentences():
    graph = EvidenceGraph()
    results = [_make_result("http://b.com", "sourceB", "Short", "Too short.")]
    graph.build_from_results(results)
    assert len(graph.claims) == 0


# ── Bloco 2: Detecção de Relações ─────────────────────────────────────────────

def test_detect_confirms_relation():
    graph = EvidenceGraph(confirm_threshold=0.30, contradict_threshold=0.20)
    
    claim_text = (
        "Studies show that AI adoption in enterprises increased significantly "
        "in 2024, driven by productivity gains and cost reduction strategies."
    )
    
    a = Claim(id="a1", text=claim_text, source="http://a.com",
              source_name="sourceA", confidence=0.9,
              tokens=graph._tokenize(claim_text))
    b = Claim(id="b1", text=claim_text, source="http://b.com",   # fonte diferente, mesmo conteúdo
              source_name="sourceB", confidence=0.8,
              tokens=graph._tokenize(claim_text))

    relations = graph.detect_relations([a, b])
    assert len(relations) >= 1
    assert relations[0].relation_type == "CONFIRMS"


def test_detect_contradicts_relation():
    graph = EvidenceGraph(confirm_threshold=0.30, contradict_threshold=0.10)
    
    text_a = "AI adoption in enterprises increased significantly in 2024."
    text_b = "AI adoption in enterprises did not increase significantly in 2024."

    a = Claim(id="a2", text=text_a, source="http://a.com", source_name="srcA",
              confidence=0.9, tokens=graph._tokenize(text_a))
    b = Claim(id="b2", text=text_b, source="http://b.com", source_name="srcB",
              confidence=0.9, tokens=graph._tokenize(text_b))

    relations = graph.detect_relations([a, b])
    contradicts = [r for r in relations if r.relation_type == "CONTRADICTS"]
    assert len(contradicts) >= 1


def test_no_self_source_relations():
    graph = EvidenceGraph(confirm_threshold=0.10, contradict_threshold=0.05)
    
    text = "AI adoption increased substantially in 2024 across all industry segments."
    a = Claim(id="x1", text=text, source="http://same.com", source_name="same",
              confidence=0.9, tokens=graph._tokenize(text))
    b = Claim(id="x2", text=text, source="http://same.com", source_name="same",
              confidence=0.9, tokens=graph._tokenize(text))
    
    # Mesma fonte — não deve gerar relações
    relations = graph.detect_relations([a, b])
    assert len(relations) == 0


# ── Bloco 3: Similaridade Jaccard ─────────────────────────────────────────────

def test_jaccard_identical():
    graph = EvidenceGraph()
    tokens = ["artificial", "intelligence", "adoption", "enterprises"]
    assert graph._compute_similarity(tokens, tokens) == 1.0


def test_jaccard_empty():
    graph = EvidenceGraph()
    assert graph._compute_similarity([], ["a", "b"]) == 0.0


def test_jaccard_no_overlap():
    graph = EvidenceGraph()
    assert graph._compute_similarity(["foo", "bar"], ["baz", "qux"]) == 0.0


# ── Bloco 4: Exportadores ─────────────────────────────────────────────────────

def test_export_graphviz():
    graph = EvidenceGraph()
    text = ("Studies show that AI adoption in enterprises increased by 45 percent in "
            "2024, driven by productivity gains across all industry sectors worldwide.")
    graph.claims = [
        Claim(id="a1", text=text, source="http://a.com",
              source_name="srcA", confidence=0.9, tokens=graph._tokenize(text))
    ]
    dot = graph.export_graphviz()
    assert "digraph EvidenceGraph" in dot
    assert "a1" in dot


def test_export_d3_json():
    graph = EvidenceGraph()
    text = ("Studies show that AI adoption in enterprises increased by 45 percent in "
            "2024, driven by productivity gains across all industry sectors worldwide.")
    graph.claims = [
        Claim(id="b1", text=text, source="http://b.com",
              source_name="srcB", confidence=0.7, tokens=[])
    ]
    graph.relations = [
        ClaimRelation(from_id="b1", to_id="b1", relation_type="CONFIRMS", weight=0.9)
    ]
    d3 = graph.export_d3_json()
    assert "nodes" in d3
    assert "links" in d3
    assert d3["nodes"][0]["id"] == "b1"


def test_export_cytoscape_json():
    graph = EvidenceGraph()
    text = ("Studies show that AI adoption in enterprises increased by 45 percent in "
            "2024, driven by productivity gains across all industry sectors worldwide.")
    graph.claims = [
        Claim(id="c1", text=text, source="http://c.com",
              source_name="srcC", confidence=0.6, tokens=[])
    ]
    graph.relations = []
    cy = graph.export_cytoscape_json()
    assert "elements" in cy
    assert cy["elements"][0]["group"] == "nodes"


def test_summary_empty():
    graph = EvidenceGraph()
    graph.claims = []
    graph.relations = []
    assert graph.summary() == ""
