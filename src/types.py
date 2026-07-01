from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime
from enum import Enum


class Domain(str, Enum):
    SAAS_B2B = "saas_b2b"
    DEV_TOOLS = "dev_tools"
    AI_ML = "ai_ml"
    AUTOMATION = "automation"
    INFRASTRUCTURE = "infrastructure"
    OPEN_SOURCE = "open_source"
    GENERAL = "general"


class Intention(str, Enum):
    DISCOVER = "discover"
    COMPARE = "compare"
    LEARN = "learn"
    IMPLEMENT = "implement"
    EVALUATE = "evaluate"


class ReportFormat(str, Enum):
    MARKDOWN = "markdown"
    PDF      = "pdf"
    DOCX     = "docx"
    PPTX     = "pptx"


@dataclass
class IntentResult:
    domain: Domain
    entities: List[str]
    intention: Intention
    urgency: str  # "sim" | "nao"
    confidence: str  # "alta" | "media" | "baixa"


@dataclass
class ExpandedQuery:
    query: str
    type: str  # "sinonimo" | "qualificador" | "plataforma" | "comparacao" | "caso_de_uso"
    priority: str  # "alta" | "media" | "baixa"
    rationale: str


@dataclass
class SearchResult:
    source: str
    title: str
    url: str
    description: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=datetime.now)
    confidence_score: float = 0.0
    evidence_quality: str = "unknown"
    citations: List[str] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    hallucination_flags: List[str] = field(default_factory=list)


@dataclass
class RankedResult(SearchResult):
    score: float = 0.0
    score_breakdown: Dict[str, float] = field(default_factory=dict)


@dataclass
class SourcePlan:
    sources: Dict[str, List[ExpandedQuery]]
    primary: List[str]
    secondary: List[str]


@dataclass
class GapAnalysis:
    is_complete: bool
    missing_aspects: List[str]
    new_queries: List[str]
    confidence: str
    rationale: str


class Verdict(str, Enum):
    """Veredito interpretável do resultado sintetizado, inspirado no Tino."""
    FOCA = "Foca"          # score >= 75 — ler/avaliar agora
    CONSIDERA = "Considera"  # score >= 50 — relevante, sem urgência
    ACOMPANHA = "Acompanha"  # score >= 30 — tangencial, revisitar
    IGNORA = "Ignora"        # score < 30  — fora do contexto


@dataclass
class SynthesizedResult:
    entity: str
    title: str
    description: str
    sources: List[str]
    urls: List[str]
    combined_score: float
    metrics: Dict[str, Any]
    highlights: List[str]
    first_seen: datetime
    last_seen: datetime
    # Campos de veredito rico (P1)
    verdict: str = ""          # "Foca" | "Considera" | "Acompanha" | "Ignora"
    tldr: str = ""             # Uma frase: o que é e por que importa
    next_step: str = ""        # Ação concreta recomendada
    read_min: int = 0          # Tempo estimado de leitura em minutos
    evidence_quality: str = "unknown"
    hallucination_flags: List[str] = field(default_factory=list)


@dataclass
class ResearchMetadata:
    query: str
    domain: str
    sources: List[str]
    total_results: int
    iterations: int
    timestamp: datetime
    duration_seconds: float
    overall_confidence: float = 0.0
    low_confidence_warnings: List[str] = field(default_factory=list)
