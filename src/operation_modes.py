"""
operation_modes.py — Presets de Operação do Smart Research Agent

6 modos pré-configurados com trade-offs distintos de velocidade vs precisão.
Integrado ao Orchestrator, CLI (main.py) e MCP Server para seleção dinâmica.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class OperationConfig:
    """Configuração completa de um modo de operação."""
    name: str
    description: str
    searchers: List[str]
    scrapers: List[str]
    confidence_threshold: float
    max_depth: int
    enable_auditor: bool
    enable_race: bool
    proxy_strategy: str
    cache_strategy: str
    timeout_seconds: int
    cost_optimization: bool
    # Bloco 3.1 — Multi-Agent Debate
    enable_debate: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "searchers": self.searchers,
            "scrapers": self.scrapers,
            "confidence_threshold": self.confidence_threshold,
            "max_depth": self.max_depth,
            "enable_auditor": self.enable_auditor,
            "enable_race": self.enable_race,
            "proxy_strategy": self.proxy_strategy,
            "cache_strategy": self.cache_strategy,
            "timeout_seconds": self.timeout_seconds,
            "cost_optimization": self.cost_optimization,
            "enable_debate": self.enable_debate,
        }


class OperationModes:
    """
    Registro central de modos de operação do SRA.

    Uso:
        config = OperationModes.get_mode("cirurgia")
        orchestrator.apply_mode(config)
    """

    MODES: Dict[str, OperationConfig] = {

        "guerrilha": OperationConfig(
            name="guerrilha",
            description="Máxima velocidade — pesquisas rápidas sem deep research. "
                        "Ideal para consultas factuais simples com prazo curto.",
            searchers=["google", "brave", "searxng", "duckduckgo"],
            scrapers=["firecrawl", "jina", "curl_impersonate"],
            confidence_threshold=0.50,
            max_depth=1,
            enable_auditor=False,
            enable_race=True,
            proxy_strategy="rotate_fast",
            cache_strategy="aggressive",
            timeout_seconds=30,
            cost_optimization=True,
        ),

        "cirurgia": OperationConfig(
            name="cirurgia",
            description="Máxima precisão — auditoria cruzada e verificação de cada claim. "
                        "Indicado para pesquisas que exigem alta confiabilidade.",
            searchers=["google", "brave", "arxiv", "github", "stackoverflow", "hackernews", "reddit"],
            scrapers=["firecrawl", "spider", "steel", "jina", "scrapingbee", "zenrows"],
            confidence_threshold=0.85,
            max_depth=3,
            enable_auditor=True,
            enable_race=True,
            proxy_strategy="rotate_careful",
            cache_strategy="minimal",
            timeout_seconds=300,
            cost_optimization=False,
        ),

        "radar": OperationConfig(
            name="radar",
            description="Monitoramento contínuo — alerta quando novas informações surgem. "
                        "Focado em trending, lançamentos e notícias recentes.",
            searchers=["google", "brave", "hackernews", "reddit", "producthunt"],
            scrapers=["firecrawl", "jina"],
            confidence_threshold=0.60,
            max_depth=1,
            enable_auditor=False,
            enable_race=True,
            proxy_strategy="rotate_fast",
            cache_strategy="aggressive",
            timeout_seconds=60,
            cost_optimization=True,
        ),

        "arqueologia": OperationConfig(
            name="arqueologia",
            description="Foco em conteúdo histórico — Wayback Machine, documentação antiga e versões legadas. "
                        "Útil para rastrear deprecações e comportamentos históricos.",
            searchers=["wayback", "github", "stackoverflow", "google"],
            scrapers=["wayback", "firecrawl", "jina"],
            confidence_threshold=0.40,
            max_depth=2,
            enable_auditor=True,
            enable_race=False,
            proxy_strategy="static",
            cache_strategy="permanent",
            timeout_seconds=120,
            cost_optimization=True,
        ),

        "concorrencia": OperationConfig(
            name="concorrencia",
            description="Inteligência competitiva — ProductHunt, GitHub trends, HN e Reddit. "
                        "Ideal para mapear o ecossistema de produtos e projetos concorrentes.",
            searchers=["producthunt", "hackernews", "reddit", "github", "google", "brave"],
            scrapers=["firecrawl", "jina", "scrapingbee"],
            confidence_threshold=0.60,
            max_depth=2,
            enable_auditor=False,
            enable_race=True,
            proxy_strategy="rotate_fast",
            cache_strategy="moderate",
            timeout_seconds=90,
            cost_optimization=True,
        ),

        "black_ops": OperationConfig(
            name="black_ops",
            description="Modo hardcore — proxies residenciais + móveis, 5-7 scrapers paralelos, "
                        "deep research com auditoria iterativa. Cobertura máxima, custo máximo.",
            searchers=[
                "google", "brave", "searxng", "arxiv", "github",
                "stackoverflow", "hackernews", "reddit", "producthunt", "devto", "medium",
            ],
            scrapers=[
                "firecrawl", "spider", "steel", "jina",
                "scrapingbee", "scrapingant", "zenrows", "curl_impersonate",
            ],
            confidence_threshold=0.90,
            max_depth=4,
            enable_auditor=True,
            enable_race=True,
            proxy_strategy="all_proxies",
            cache_strategy="minimal",
            timeout_seconds=600,
            cost_optimization=False,
        ),

        # ── Bloco 3.1 ─────────────────────────────────────────────────────────
        "debate": OperationConfig(
            name="debate",
            description="Multi-Agent Debate — gera hipóteses opostas e as testa com pesquisa paralela. "
                        "Um juiz LLM avalia os argumentos e decide o vencedor. "
                        "Ideal para questões controversas, comparações e decisões estratégicas.",
            searchers=["google", "brave", "arxiv", "github", "stackoverflow", "hackernews", "reddit"],
            scrapers=["firecrawl", "jina"],
            confidence_threshold=0.75,
            max_depth=2,
            enable_auditor=True,
            enable_race=True,
            proxy_strategy="rotate_careful",
            cache_strategy="minimal",
            timeout_seconds=240,
            cost_optimization=False,
            enable_debate=True,
        ),
    }

    # Modo padrão quando nenhum modo é especificado
    DEFAULT_MODE = "cirurgia"

    # ── API pública ────────────────────────────────────────────────────────────

    @classmethod
    def get_mode(cls, mode_name: str) -> OperationConfig:
        """Retorna a config do modo solicitado; fallback para 'cirurgia'."""
        mode = cls.MODES.get(mode_name)
        if mode is None:
            logger.warning(
                f"OperationModes: modo '{mode_name}' desconhecido. "
                f"Usando fallback '{cls.DEFAULT_MODE}'."
            )
            return cls.MODES[cls.DEFAULT_MODE]
        return mode

    @classmethod
    def list_modes(cls) -> List[str]:
        """Lista todos os nomes de modos disponíveis."""
        return list(cls.MODES.keys())

    @classmethod
    def get_mode_description(cls, mode_name: str) -> str:
        """Retorna a descrição de um modo específico."""
        mode = cls.MODES.get(mode_name)
        return mode.description if mode else "Modo não encontrado."

    @classmethod
    def auto_select(cls, query: str) -> str:
        """
        Seleciona automaticamente o modo mais adequado com base em palavras-chave da query.

        Heurística simples para casos onde o modo não é especificado pelo usuário.
        """
        q = query.lower()

        if any(kw in q for kw in ["rápido", "rapido", "rápida", "rapida", "resumo", "quick", "fast", "summary"]):
            return "guerrilha"

        if any(kw in q for kw in ["verificar", "verify", "fact-check", "confiança", "confianca", "evidência", "evidencia"]):
            return "cirurgia"

        if any(kw in q for kw in ["novidade", "novidades", "trending", "lançamento", "lancamento", "launch", "release", "news"]):
            return "radar"

        if any(kw in q for kw in ["histórico", "historico", "legado", "deprecated", "antigo", "wayback", "legacy"]):
            return "arqueologia"

        if any(kw in q for kw in ["concorrente", "competitor", "alternativa", "alternative", "versus", "vs"]):
            return "concorrencia"

        if any(kw in q for kw in ["completo", "exhaustive", "deep", "profundo", "tudo sobre"]):
            return "black_ops"

        return cls.DEFAULT_MODE

    @classmethod
    def get_all_descriptions(cls) -> Dict[str, str]:
        """Retorna dicionário {nome: descrição} de todos os modos."""
        return {name: cfg.description for name, cfg in cls.MODES.items()}
