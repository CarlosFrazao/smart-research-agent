"""
operation_modes.py — Presets de Operação do Smart Research Agent

6 modos pré-configurados com trade-offs distintos de velocidade vs precisão.
Integrado ao Orchestrator, CLI (main.py) e MCP Server para seleção dinâmica.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OperationConfig:
    """Configuração completa de um modo de operação."""

    name: str
    description: str
    searchers: list[str]
    scrapers: list[str]
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
    # Bloco 3.2 — Active Personas
    active_personas: list[str] = field(default_factory=list)
    # Bloco 3.3 — FASE 3: Passada Adversarial (anti viés de confirmação)
    enable_adversarial_pass: bool = False

    def to_dict(self) -> dict[str, Any]:
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
            "active_personas": self.active_personas,
            "enable_adversarial_pass": self.enable_adversarial_pass,
        }


class OperationModes:
    """
    Registro central de modos de operação do SRA.

    Uso:
        config = OperationModes.get_mode("cirurgia")
        orchestrator.apply_mode(config)
    """

    MODES: dict[str, OperationConfig] = {
        "guerrilha": OperationConfig(
            name="guerrilha",
            description="Máxima velocidade — pesquisas rápidas sem deep research. "
            "Ideal para consultas factuais simples com prazo curto.",
            searchers=["web", "searxng", "serpapi"],
            scrapers=["firecrawl", "jina", "curl_impersonate", "playwright"],
            confidence_threshold=0.50,
            max_depth=1,
            enable_auditor=False,
            enable_race=True,
            proxy_strategy="rotate_fast",
            cache_strategy="aggressive",
            timeout_seconds=30,
            cost_optimization=True,
            active_personas=[],
        ),
        "cirurgia": OperationConfig(
            name="cirurgia",
            description="Máxima precisão — auditoria cruzada e verificação de cada claim. "
            "Indicado para pesquisas que exigem alta confiabilidade.",
            searchers=[
                "web",
                "arxiv",
                "github",
                "stackoverflow",
                "hackernews",
                "reddit",
                "serpapi",
            ],
            scrapers=["firecrawl", "spider", "steel", "jina", "scrapingbee", "zenrows"],
            confidence_threshold=0.85,
            max_depth=3,
            enable_auditor=True,
            enable_race=True,
            proxy_strategy="rotate_careful",
            cache_strategy="minimal",
            timeout_seconds=300,
            cost_optimization=False,
            active_personas=["prism_scientist"],
            enable_adversarial_pass=True,
        ),
        "radar": OperationConfig(
            name="radar",
            description="Monitoramento contínuo — alerta quando novas informações surgem. "
            "Focado em trending, lançamentos e notícias recentes.",
            searchers=["web", "hackernews", "reddit", "producthunt"],
            scrapers=["firecrawl", "jina"],
            confidence_threshold=0.60,
            max_depth=1,
            enable_auditor=False,
            enable_race=True,
            proxy_strategy="rotate_fast",
            cache_strategy="aggressive",
            timeout_seconds=60,
            cost_optimization=False,
            active_personas=["sage_strategy"],
        ),
        "arqueologia": OperationConfig(
            name="arqueologia",
            description="Foco em conteúdo histórico — Wayback Machine, documentação antiga e versões legadas. "
            "Útil para rastrear deprecações e comportamentos históricos.",
            searchers=["wayback", "github", "stackoverflow", "web"],
            scrapers=["wayback", "firecrawl", "jina"],
            confidence_threshold=0.40,
            max_depth=2,
            enable_auditor=True,
            enable_race=False,
            proxy_strategy="static",
            cache_strategy="permanent",
            timeout_seconds=120,
            cost_optimization=True,
            active_personas=["scout_explorer"],
            enable_adversarial_pass=True,
        ),
        "concorrencia": OperationConfig(
            name="concorrencia",
            description="Inteligência competitiva — ProductHunt, GitHub trends, HN e Reddit. "
            "Ideal para mapear o ecossistema de produtos e projetos concorrentes.",
            searchers=[
                "producthunt",
                "hackernews",
                "reddit",
                "github",
                "web",
            ],
            scrapers=["firecrawl", "jina", "scrapingbee"],
            confidence_threshold=0.60,
            max_depth=2,
            enable_auditor=False,
            enable_race=True,
            proxy_strategy="rotate_fast",
            cache_strategy="moderate",
            timeout_seconds=90,
            cost_optimization=False,
            active_personas=["sage_strategy", "scout_explorer"],
        ),
        "black_ops": OperationConfig(
            name="black_ops",
            description="Modo hardcore — proxies residenciais + móveis, 5-7 scrapers paralelos, "
            "deep research com auditoria iterativa. Cobertura máxima, custo máximo.",
            searchers=[
                "web",
                "searxng",
                "arxiv",
                "github",
                "stackoverflow",
                "hackernews",
                "reddit",
                "producthunt",
                "serpapi",
            ],
            scrapers=[
                "firecrawl",
                "spider",
                "steel",
                "jina",
                "scrapingbee",
                "scrapingant",
                "zenrows",
                "curl_impersonate",
                "playwright",
            ],
            confidence_threshold=0.90,
            max_depth=4,
            enable_auditor=True,
            enable_race=True,
            proxy_strategy="all_proxies",
            cache_strategy="minimal",
            timeout_seconds=600,
            cost_optimization=False,
            active_personas=["sage_strategy", "prism_scientist", "scout_explorer"],
            enable_adversarial_pass=True,
        ),
        # ── Bloco 3.1 ─────────────────────────────────────────────────────────
        "debate": OperationConfig(
            name="debate",
            description="Multi-Agent Debate — gera hipóteses opostas e as testa com pesquisa paralela. "
            "Um juiz LLM avalia os argumentos e decide o vencedor. "
            "Ideal para questões controversas, comparações e decisões estratégicas.",
            searchers=[
                "web",
                "arxiv",
                "github",
                "stackoverflow",
                "hackernews",
                "reddit",
                "serpapi",
            ],
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
            active_personas=["prism_scientist"],
        ),
        # ── GAP 2: modo acadêmico/biomédico ───────────────────────────────────
        "academico": OperationConfig(
            name="academico",
            description="Pesquisa acadêmica e biomédica — PubMed, arXiv, Semantic Scholar e web. "
            "Ideal para literatura científica, ensaios clínicos, revisões sistemáticas e "
            "fundamentação teórica. Torna o PubMed alcançável via CLI (ver PLANO_FECHAR_GAPS.md).",
            searchers=[
                "pubmed",
                "arxiv",
                "semantic_scholar",
                "crossref",
                "clinicaltrials",
                "web",
                "searxng",
            ],
            scrapers=["firecrawl", "jina"],
            confidence_threshold=0.80,
            max_depth=3,
            enable_auditor=True,
            enable_race=True,
            proxy_strategy="rotate_careful",
            cache_strategy="minimal",
            timeout_seconds=300,
            cost_optimization=False,
            active_personas=["prism_scientist"],
            enable_adversarial_pass=True,
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
    def list_modes(cls) -> list[str]:
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
        selected = cls.DEFAULT_MODE

        if any(
            kw in q
            for kw in [
                "rápido",
                "rapido",
                "rápida",
                "rapida",
                "resumo",
                "quick",
                "fast",
                "summary",
            ]
        ):
            selected = "guerrilha"

        elif any(
            kw in q
            for kw in [
                "verificar",
                "verify",
                "fact-check",
                "confiança",
                "confianca",
                "evidência",
                "evidencia",
            ]
        ):
            selected = "cirurgia"

        elif any(
            kw in q
            for kw in [
                "novidade",
                "novidades",
                "trending",
                "lançamento",
                "lancamento",
                "launch",
                "release",
                "news",
            ]
        ):
            selected = "radar"

        elif any(
            kw in q
            for kw in [
                "histórico",
                "historico",
                "legado",
                "deprecated",
                "antigo",
                "wayback",
                "legacy",
            ]
        ):
            selected = "arqueologia"

        elif any(
            kw in q
            for kw in [
                "concorrente",
                "competitor",
                "alternativa",
                "alternative",
                "versus",
                "vs",
            ]
        ):
            selected = "concorrencia"

        elif any(
            kw in q
            for kw in ["completo", "exhaustive", "deep", "profundo", "tudo sobre"]
        ):
            selected = "black_ops"

        elif any(
            kw in q
            for kw in [
                "pubmed",
                "médico",
                "medico",
                "clínico",
                "clinico",
                "clinical",
                "trial",
                "ensaios",
                "ensayo",
                "biomed",
                "biomédico",
                "biomedico",
                "doi",
                "health",
                "saúde",
                "saude",
                "literatura",
                "systematic review",
                "revisão sistemática",
                "revisao sistematica",
            ]
        ):
            selected = "academico"

        if selected not in cls.MODES:
            logger.warning(
                f"OperationModes.auto_select selecionou preset inválido '{selected}'. Fallback para '{cls.DEFAULT_MODE}'."
            )
            return cls.DEFAULT_MODE

        return selected

    @classmethod
    def get_all_descriptions(cls) -> dict[str, str]:
        """Retorna dicionário {nome: descrição} de todos os modos."""
        return {name: cfg.description for name, cfg in cls.MODES.items()}

    @classmethod
    def validate_operation_modes(cls) -> None:
        """
        Valida a consistência de todos os modos de operação.
        Levanta ValueError com detalhes se alguma configuração for inválida.
        Deve ser chamado no startup ou em testes de sanidade.
        """
        errors: list[str] = []
        for mode_name, cfg in cls.MODES.items():
            if not cfg.searchers:
                errors.append(f"Modo '{mode_name}': lista de searchers está vazia.")
            if cfg.confidence_threshold < 0.0 or cfg.confidence_threshold > 1.0:
                errors.append(
                    f"Modo '{mode_name}': confidence_threshold={cfg.confidence_threshold} fora do intervalo [0.0, 1.0]."
                )
            if cfg.max_depth <= 0:
                errors.append(
                    f"Modo '{mode_name}': max_depth={cfg.max_depth} deve ser > 0."
                )
            if cfg.timeout_seconds <= 0:
                errors.append(
                    f"Modo '{mode_name}': timeout_seconds={cfg.timeout_seconds} deve ser > 0."
                )
            if cfg.proxy_strategy not in (
                "rotate",
                "rotate_fast",
                "rotate_careful",
                "all_proxies",
                "fixed",
                "direct",
                "vps_first",
                "static",
                "none",
            ):
                errors.append(
                    f"Modo '{mode_name}': proxy_strategy='{cfg.proxy_strategy}' não é um valor reconhecido."
                )
            if cfg.cache_strategy not in (
                "always",
                "smart",
                "never",
                "minimal",
                "aggressive",
                "moderate",
                "permanent",
            ):
                errors.append(
                    f"Modo '{mode_name}': cache_strategy='{cfg.cache_strategy}' deve ser 'always', 'smart' ou 'never'."
                )
        if errors:
            raise ValueError(
                f"Configuração inválida em {len(errors)} modo(s) de operação:\n"
                + "\n".join(f"  • {e}" for e in errors)
            )
        logger.info(
            f"[validate_operation_modes] {len(cls.MODES)} modos validados com sucesso."
        )
