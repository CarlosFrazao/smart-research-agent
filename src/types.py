"""Tipos e modelos centrais do Smart Research Agent.

Define todas as estruturas de dados compartilhadas entre os módulos:
domínio/intenção (enums), resultados de busca, resultados sintetizados,
plano de fontes, análise de lacunas e metadados de pesquisa.

Todos os modelos herdam de `SRAModel` (Pydantic `BaseModel`) e ganham,
de graça, em relação às antigas `@dataclass`:

- Validação de tipos e de faixas de valores na construção *e* na atribuição
  (``model_config.validate_assignment=True``), incluindo os pontos do código
  onde os campos são mutados após a criação (ex: ``confidence_scorer.py``,
  ``link_verifier.py``).
- Parsing automático de tipos compostos (ex: strings ISO-8601 são
  convertidas para ``datetime`` automaticamente ao reidratar resultados
  cacheados via ``SearchResult(**dict_cacheado)``).
- Mensagens de erro estruturadas (`ValidationError`) quando um LLM devolve
  um JSON fora do schema esperado — os `try/except Exception` já existentes
  em `intent_analyzer.py`, `query_expander.py`, `gap_detector.py` etc.
  continuam funcionando como rede de segurança, mas agora capturam
  divergências de schema, e não só erros de parsing de JSON.

Notas de compatibilidade (decisões deliberadas, não omissões):

- ``model_config.extra = "ignore"``: os resultados de busca são persistidos
  em cache (`src/cache`) e depois reconstruídos via ``SearchResult(**r)``.
  Usar ``"forbid"`` quebraria a leitura de cache gravado por uma versão
  anterior do modelo (campo removido/renomeado). ``"ignore"`` preserva
  compatibilidade retroativa sem abrir mão da validação dos campos que
  *são* conhecidos.
- ``ExpandedQuery.type`` permanece ``str`` livre (não ``Literal``): embora o
  docstring original sugerisse um conjunto fixo, o código real usa valores
  bem mais amplos e específicos de contexto — ``"synonym"``, ``"perspective"``,
  ``"evidence"``, ``"community"``, ``"academic"`` (prompt do `QueryExpander`),
  ``"original"``, ``"temporal"`` (fallback local), ``"gap_fill"``
  (`orchestrator.py`), ``"fact_check"`` (`conflict_detector.py`) e
  ``"debate_hypothesis"`` (`debate_orchestrator.py`). Restringir a um
  ``Literal`` quebraria vários desses fluxos.
- ``SynthesizedResult.combined_score`` não tem teto (`le`) apesar de a
  escala nominal ser 0-100: `tests/test_research_score_fix.py` constrói
  deliberadamente um `combined_score=500.0` ("Score absurdo") para validar
  que `ResearchScoreAggregator` normaliza/clampa defensivamente valores
  corrompidos vindos de fontes externas. Adicionar `le=100` moveria essa
  falha de "clampada silenciosamente pelo agregador" para "pipeline inteiro
  aborta na construção do resultado", destruindo uma pesquisa já concluída
  (o resultado é montado na Etapa 9/9 do `orchestrator.py`, fora de qualquer
  bloco `try/except`). Mantemos apenas `ge=0.0`.
- ``ResearchMetadata.domain`` permanece ``str`` (não ``Domain``) pelo mesmo
  motivo de robustez: é construído no último passo do pipeline
  (`orchestrator.py`, fora de `try/except`) a partir de ``intent.domain.value``,
  que já foi validado como `Domain` na etapa de `IntentAnalyzer`. Reforçar a
  validação aqui não pega nenhum bug real, mas adiciona um ponto de falha
  tardio e caro caso o enum `Domain` ganhe/perca valores no futuro.
"""
# HACK: Evita conflito de shadowing com o módulo 'types' da standard library.
# Como a pasta 'src' está no sys.path (ex: devido a instalação editável do setuptools),
# qualquer 'import types' feito por bibliotecas padrão (ex: enum, functools)
# tenta carregar este arquivo (src/types.py) como o módulo 'types' global, gerando import circular.
# Para evitar isso, removemos temporariamente 'src' do sys.path para forçar a carga do 'types' real,
# salvando-o em sys.modules['types'].
import sys
if 'types' not in sys.modules or not hasattr(sys.modules['types'], 'MappingProxyType'):
    import os
    saved_path = list(sys.path)
    sys.path = [p for p in sys.path if not p.endswith('src') and p != os.path.abspath('src')]
    if 'types' in sys.modules:
        del sys.modules['types']
    import types as _std_types
    sys.modules['types'] = _std_types
    sys.path = saved_path

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SRAModel(BaseModel):
    """Configuração Pydantic compartilhada por todos os modelos do SRA.

    - ``validate_assignment=True``: os campos são revalidados quando
      mutados após a criação (padrão comum no código, ex:
      ``result.confidence_score = round(...)`` em `confidence_scorer.py`).
    - ``str_strip_whitespace=True``: remove espaços acidentais de strings
      (títulos/descrições vindos de scraping costumam trazer espaços extras).
    - ``extra="ignore"``: tolera campos desconhecidos ao reidratar dados
      cacheados de versões anteriores do modelo (ver nota de compatibilidade
      no topo do módulo).
    """

    model_config = ConfigDict(
        validate_assignment=True,
        str_strip_whitespace=True,
        extra="ignore",
    )


class Domain(StrEnum):
    """Domínios de pesquisa suportados pelo Smart Research Agent."""

    SAAS_B2B = "saas_b2b"
    DEV_TOOLS = "dev_tools"
    AI_ML = "ai_ml"
    AUTOMATION = "automation"
    INFRASTRUCTURE = "infrastructure"
    OPEN_SOURCE = "open_source"
    GENERAL = "general"


class Intention(StrEnum):
    """Intenções de pesquisa detectadas pelo `IntentAnalyzer`."""

    DISCOVER = "discover"
    COMPARE = "compare"
    LEARN = "learn"
    IMPLEMENT = "implement"
    EVALUATE = "evaluate"


class ReportFormat(StrEnum):
    """Formatos de exportação de relatórios suportados."""

    MARKDOWN = "markdown"
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"


class Verdict(StrEnum):
    """Veredito interpretável do resultado sintetizado, inspirado no Tino."""

    FOCA = "Foca"  # score >= 75 — ler/avaliar agora
    CONSIDERA = "Considera"  # score >= 50 — relevante, sem urgência
    ACOMPANHA = "Acompanha"  # score >= 30 — tangencial, revisitar
    IGNORA = "Ignora"  # score < 30  — fora do contexto


# Níveis de confiança/urgência usados de forma consistente pelos prompts de
# LLM (`intent_analyzer.md`, `gap_detector.md`) e pelas heurísticas locais.
ConfidenceLevel = Literal["alta", "media", "baixa"]
UrgencyFlag = Literal["sim", "nao"]
PriorityLevel = Literal["alta", "media", "baixa"]
EvidenceQuality = Literal["verified", "cited", "inferred", "unknown"]


class IntentResult(SRAModel):
    """Resultado da análise de intenção de uma query de pesquisa.

    Schema também usado para validar o JSON estruturado devolvido pelo LLM
    em `IntentAnalyzer.analyze` (`src/intent_analyzer.py`): uma resposta que
    fuja do schema (ex: ``confidence: "high"`` em inglês) levanta
    `ValidationError`, capturada pelo `except Exception` existente, que já
    faz fallback para o resultado heurístico.

    Attributes:
        domain: Domínio classificado (ex: ``Domain.AI_ML``).
        entities: Entidades extraídas da query (nomes, repos GitHub, etc).
        intention: Intenção detectada (descoberta, comparação, implementação, etc).
        urgency: ``"sim"`` se há sinais de urgência/recência, ``"nao"`` caso contrário.
        confidence: Nível de confiança da classificação (``"alta"``, ``"media"``, ``"baixa"``).
    """

    domain: Domain
    entities: list[str] = Field(default_factory=list)
    intention: Intention
    urgency: UrgencyFlag
    confidence: ConfidenceLevel


class ExpandedQuery(SRAModel):
    """Query expandida gerada pelo `QueryExpander` a partir da query original.

    Também usada por `GapDetector`/`orchestrator.py` (``type="gap_fill"``),
    `ConflictDetector` (``type="fact_check"``) e `DebateOrchestrator`
    (``type="debate_hypothesis"``) para reaproveitar o mesmo contrato de
    "próxima busca a executar".

    Attributes:
        query: Texto da query expandida.
        type: Tipo de expansão. Campo livre por design — ver nota de
            compatibilidade no topo do módulo.
        priority: Prioridade de execução (``"alta"``, ``"media"``, ``"baixa"``).
        rationale: Justificativa para a expansão.
    """

    query: str = Field(min_length=1)
    type: str = Field(min_length=1)
    priority: PriorityLevel = "media"
    rationale: str = ""

    @field_validator("query")
    @classmethod
    def _query_nao_vazia(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query não pode ser vazia ou apenas espaços")
        return v


class SearchResult(SRAModel):
    """Resultado bruto retornado por um searcher.

    Attributes:
        source: Nome do searcher de origem (ex: ``"github"``, ``"reddit"``).
        title: Título do resultado.
        url: URL do resultado.
        description: Descrição ou snippet do resultado.
        metrics: Métricas específicas da fonte (stars, upvotes, points, etc).
        raw: Payload bruto da API de origem.
        fetched_at: Timestamp de coleta.
        confidence_score: Score de confiança calculado (0.0-1.0).
        evidence_quality: Qualidade da evidência.
        citations: URLs citadas como referência.
        contradictions: Descrições de contradições detectadas.
        hallucination_flags: Sinalizadores de possível alucinação.
    """

    source: str = Field(min_length=1)
    # title/url/description aceitam string vazia de propósito: vários
    # searchers (ex: rss_searcher.py) usam SearchResult(title="", url="",
    # description="") como placeholder de "sem resultado"/erro controlado.
    title: str = ""
    url: str = ""
    description: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=datetime.now)
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_quality: EvidenceQuality = "unknown"
    citations: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    hallucination_flags: list[str] = Field(default_factory=list)


class RankedResult(SearchResult):
    """Resultado de busca enriquecido com score de qualidade após ranqueamento.

    Attributes:
        score: Score de qualidade calculado pelo `QualityRanker` (0-100).
        score_breakdown: Detalhamento do score por dimensão.
    """

    score: float = Field(default=0.0, ge=0.0, le=100.0)
    # `dict[str, float]` no dataclass original era impreciso: `ranker.py`
    # grava tanto números (``base_score``, ``misinformation_penalty``) quanto
    # texto (``misinformation_reason``) nesse dicionário. Dataclasses não
    # validam tipos em runtime, então isso nunca foi detectado; com Pydantic
    # o tipo precisa refletir a realidade para não rejeitar dados válidos.
    score_breakdown: dict[str, float | str | None] = Field(default_factory=dict)


class SourcePlan(SRAModel):
    """Plano de distribuição de buscas por searcher gerado pelo `SourcePlanner`.

    Attributes:
        sources: Mapa de nome-do-searcher para lista de `ExpandedQuery` a executar.
        primary: Searchers de alta prioridade para o domínio detectado.
        secondary: Searchers de suporte e enriquecimento.
    """

    sources: dict[str, list[ExpandedQuery]] = Field(default_factory=dict)
    primary: list[str] = Field(default_factory=list)
    secondary: list[str] = Field(default_factory=list)


class GapAnalysis(SRAModel):
    """Resultado da análise de lacunas realizada pelo `GapDetector`.

    Schema também usado para validar o JSON estruturado devolvido pelo LLM
    em `GapDetector.detect` (``GapAnalysis(**result)``); uma resposta fora
    do schema é capturada pelo `except Exception` já existente, que faz
    fallback para "pesquisa suficiente".

    Attributes:
        is_complete: True se a pesquisa está considerada completa.
        missing_aspects: Lista de aspectos da query ainda não cobertos.
        new_queries: Sugestões de queries para fechar as lacunas.
        confidence: Nível de confiança da análise (``"alta"``, ``"media"``, ``"baixa"``).
        rationale: Justificativa para a classificação de completude.
    """

    is_complete: bool
    missing_aspects: list[str] = Field(default_factory=list)
    new_queries: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel
    rationale: str = ""


class SynthesizedResult(SRAModel):
    """Resultado consolidado e sintetizado de um cluster de entidades.

    Agrega resultados de múltiplas fontes sobre a mesma entidade,
    calculando score combinado e gerando veredito, TL;DR e próxima ação.

    Attributes:
        entity: Chave/slug da entidade identificada.
        title: Melhor título disponível no cluster.
        description: Melhor descrição disponível no cluster.
        sources: Lista de fontes que cobriram esta entidade.
        urls: Lista de URLs associadas.
        combined_score: Média ponderada dos scores do cluster (nominalmente
            0-100; sem teto superior — ver nota de compatibilidade no topo
            do módulo).
        metrics: Métricas consolidadas (max por campo numérico).
        highlights: Destaques notáveis (estrelas, upvotes, etc).
        first_seen: Data mais antiga de coleta no cluster (``None`` quando
            indisponível — ex: resultados sintéticos/de teste sem coleta real).
        last_seen: Data mais recente de coleta no cluster (idem).
        verdict: Veredito qualitativo (Foca, Considera, Acompanha, Ignora).
        tldr: Resumo de uma frase com contexto relevante.
        next_step: Ação concreta recomendada para este resultado.
        read_min: Estimativa de tempo de leitura em minutos.
        evidence_quality: Qualidade geral de evidência do cluster.
        hallucination_flags: Flags de alertas de qualidade/hallucination.
    """

    entity: str = Field(min_length=1)
    title: str
    description: str
    sources: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    combined_score: float = Field(ge=0.0)
    metrics: dict[str, Any] = Field(default_factory=dict)
    highlights: list[str] = Field(default_factory=list)
    # Opcional (não `datetime` puro): `research_score.py` já lida com ausência
    # via `getattr(r, "last_seen", getattr(r, "first_seen", None))`, e
    # `tests/test_sentiment_analyzer.py` constrói resultados sintéticos com
    # `first_seen=None`/`last_seen=None` propositalmente (datas de coleta são
    # irrelevantes para o que é testado ali). O dataclass original também
    # aceitava `None` nesses campos — dataclasses não validam tipos em
    # runtime — então isto preserva o comportamento observado, só que agora
    # de forma explícita no schema em vez de por ausência de validação.
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    # Campos de veredito rico (P1)
    verdict: Literal["", "Foca", "Considera", "Acompanha", "Ignora"] = ""
    tldr: str = ""
    next_step: str = ""
    read_min: int = Field(default=0, ge=0)
    evidence_quality: EvidenceQuality = "unknown"
    hallucination_flags: list[str] = Field(default_factory=list)


class ResearchMetadata(SRAModel):
    """Metadados de uma sessão de pesquisa completa.

    Attributes:
        query: Query original do usuário.
        domain: Domínio classificado pelo `IntentAnalyzer` (string livre —
            ver nota de compatibilidade no topo do módulo).
        sources: Fontes efetivamente consultadas na pesquisa.
        total_results: Total de resultados brutos coletados.
        iterations: Número de iterações de busca realizadas.
        timestamp: Timestamp de início da pesquisa.
        duration_seconds: Duração total em segundos.
        overall_confidence: Confiança média ponderada (0.0-1.0).
        low_confidence_warnings: Alertas de resultados com baixa confiança.
    """

    query: str = Field(min_length=1)
    domain: str = ""
    sources: list[str] = Field(default_factory=list)
    total_results: int = Field(default=0, ge=0)
    iterations: int = Field(default=0, ge=0)
    timestamp: datetime
    duration_seconds: float = Field(default=0.0, ge=0.0)
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    low_confidence_warnings: list[str] = Field(default_factory=list)
