"""Tipos e dataclasses centrais do Smart Research Agent.

Define todas as estruturas de dados compartilhadas entre os modulos:
dominio/intencao (enums), resultados de busca, resultados sintetizados,
plano de fontes, analise de lacunas e metadados de pesquisa.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Domain(StrEnum):
    """Dominios de pesquisa suportados pelo Smart Research Agent."""

    SAAS_B2B = "saas_b2b"
    DEV_TOOLS = "dev_tools"
    AI_ML = "ai_ml"
    AUTOMATION = "automation"
    INFRASTRUCTURE = "infrastructure"
    OPEN_SOURCE = "open_source"
    GENERAL = "general"


class Intention(StrEnum):
    """Intencoes de pesquisa detectadas pelo `IntentAnalyzer`."""

    DISCOVER = "discover"
    COMPARE = "compare"
    LEARN = "learn"
    IMPLEMENT = "implement"
    EVALUATE = "evaluate"


class ReportFormat(StrEnum):
    """Formatos de exportacao de relatorios suportados."""

    MARKDOWN = "markdown"
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"


@dataclass
class IntentResult:
    """Resultado da analise de intencao de uma query de pesquisa.

    Attributes:
        domain: Dominio classificado (ex: ``Domain.AI_ML``).
        entities: Entidades extraidas da query (nomes, repos GitHub, etc).
        intention: Intencao detectada (descoberta, comparacao, implementacao, etc).
        urgency: ``"sim"`` se ha sinais de urgencia/recencia, ``"nao"`` caso contrario.
        confidence: Nivel de confianca da classificacao (``"alta"``, ``"media"``, ``"baixa"``).
    """

    domain: Domain
    entities: list[str]
    intention: Intention
    urgency: str  # "sim" | "nao"
    confidence: str  # "alta" | "media" | "baixa"


@dataclass
class ExpandedQuery:
    """Query expandida gerada pelo `QueryExpander` a partir da query original.

    Attributes:
        query: Texto da query expandida.
        type: Tipo de expansao ("sinonimo", "qualificador", "plataforma", etc).
        priority: Prioridade de execucao (``"alta"``, ``"media"``, ``"baixa"``).
        rationale: Justificativa para a expansao.
    """

    query: str
    type: (
        str  # "sinonimo" | "qualificador" | "plataforma" | "comparacao" | "caso_de_uso"
    )
    priority: str  # "alta" | "media" | "baixa"
    rationale: str


@dataclass
class SearchResult:
    """Resultado bruto retornado por um searcher.

    Attributes:
        source: Nome do searcher de origem (ex: ``"github"``, ``"reddit"``).
        title: Titulo do resultado.
        url: URL do resultado.
        description: Descricao ou snippet do resultado.
        metrics: Metricas especificas da fonte (stars, upvotes, points, etc).
        raw: Payload bruto da API de origem.
        fetched_at: Timestamp de coleta.
        confidence_score: Score de confianca calculado (0.0-1.0).
        evidence_quality: Qualidade da evidencia (``"verified"``, ``"cited"``, ``"inferred"``, ``"unknown"``).
        citations: URLs citadas como referencia.
        contradictions: Descricoes de contradicoes detectadas.
        hallucination_flags: Sinalizadores de possivel alucinacao.
    """

    source: str
    title: str
    url: str
    description: str
    metrics: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=datetime.now)
    confidence_score: float = 0.0
    evidence_quality: str = "unknown"
    citations: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    hallucination_flags: list[str] = field(default_factory=list)


@dataclass
class RankedResult(SearchResult):
    """Resultado de busca enriquecido com score de qualidade apos ranqueamento.

    Attributes:
        score: Score de qualidade calculado pelo `QualityRanker` (0-100).
        score_breakdown: Detalhamento do score por dimensao.
    """

    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)


@dataclass
class SourcePlan:
    """Plano de distribuicao de buscas por searcher gerado pelo `SourcePlanner`.

    Attributes:
        sources: Mapa de nome-do-searcher para lista de `ExpandedQuery` a executar.
        primary: Searchers de alta prioridade para o dominio detectado.
        secondary: Searchers de suporte e enriquecimento.
    """

    sources: dict[str, list[ExpandedQuery]]
    primary: list[str]
    secondary: list[str]


@dataclass
class GapAnalysis:
    """Resultado da analise de lacunas realizada pelo `GapDetector`.

    Attributes:
        is_complete: True se a pesquisa esta considerada completa.
        missing_aspects: Lista de aspectos da query ainda nao cobertos.
        new_queries: Sugestoes de queries para fechar as lacunas.
        confidence: Nivel de confianca da analise (``"alta"``, ``"media"``, ``"baixa"``).
        rationale: Justificativa para a classificacao de completude.
    """

    is_complete: bool
    missing_aspects: list[str]
    new_queries: list[str]
    confidence: str
    rationale: str


class Verdict(StrEnum):
    """Veredito interpretável do resultado sintetizado, inspirado no Tino."""

    FOCA = "Foca"  # score >= 75 — ler/avaliar agora
    CONSIDERA = "Considera"  # score >= 50 — relevante, sem urgência
    ACOMPANHA = "Acompanha"  # score >= 30 — tangencial, revisitar
    IGNORA = "Ignora"  # score < 30  — fora do contexto


@dataclass
class SynthesizedResult:
    """Resultado consolidado e sintetizado de um cluster de entidades.

    Agrega resultados de multiplas fontes sobre a mesma entidade,
    calculando score combinado e gerand veredicto, TL;DR e proxima acao.

    Attributes:
        entity: Chave/slug da entidade identificada.
        title: Melhor titulo disponivel no cluster.
        description: Melhor descricao disponivel no cluster.
        sources: Lista de fontes que cobriram esta entidade.
        urls: Lista de URLs associadas.
        combined_score: Media ponderada dos scores do cluster.
        metrics: Metricas consolidadas (max por campo numerico).
        highlights: Destaques notaveis (estrelas, upvotes, etc).
        first_seen: Data mais antiga de coleta no cluster.
        last_seen: Data mais recente de coleta no cluster.
        verdict: Veredito qualitativo (Foca, Considera, Acompanha, Ignora).
        tldr: Resumo de uma frase com contexto relevante.
        next_step: Acao concreta recomendada para este resultado.
        read_min: Estimativa de tempo de leitura em minutos.
        evidence_quality: Qualidade geral de evidencia do cluster.
        hallucination_flags: Flags de alertas de qualidade/hallucination.
    """

    entity: str
    title: str
    description: str
    sources: list[str]
    urls: list[str]
    combined_score: float
    metrics: dict[str, Any]
    highlights: list[str]
    first_seen: datetime
    last_seen: datetime
    # Campos de veredito rico (P1)
    verdict: str = ""  # "Foca" | "Considera" | "Acompanha" | "Ignora"
    tldr: str = ""  # Uma frase: o que é e por que importa
    next_step: str = ""  # Ação concreta recomendada
    read_min: int = 0  # Tempo estimado de leitura em minutos
    evidence_quality: str = "unknown"
    hallucination_flags: list[str] = field(default_factory=list)


@dataclass
class ResearchMetadata:
    """Metadados de uma sessao de pesquisa completa.

    Attributes:
        query: Query original do usuario.
        domain: Dominio classificado pelo `IntentAnalyzer`.
        sources: Fontes efetivamente consultadas na pesquisa.
        total_results: Total de resultados brutos coletados.
        iterations: Numero de iteracoes de busca realizadas.
        timestamp: Timestamp de inicio da pesquisa.
        duration_seconds: Duracao total em segundos.
        overall_confidence: Confianca media ponderada (0.0-1.0).
        low_confidence_warnings: Alertas de resultados com baixa confianca.
    """

    query: str
    domain: str
    sources: list[str]
    total_results: int
    iterations: int
    timestamp: datetime
    duration_seconds: float
    overall_confidence: float = 0.0
    low_confidence_warnings: list[str] = field(default_factory=list)
