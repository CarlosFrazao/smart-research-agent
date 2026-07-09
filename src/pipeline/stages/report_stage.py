"""Report Stage - Stage independente para geração de relatórios.

Este módulo implementa um stage dedicado à geração de relatórios com:
- Paralelização de 3 seções LLM (executive summary, recommendation, trends)
- Consolidação em 1 chamada LLM com schema JSON
- Cache de seções reutilizáveis via SharedCache
- Fallback automático para chamadas individuais

Author: Smart Research Agent Team
Version: 6.3.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from src.cache.shared_cache import SharedCache
from src.agent_persona_loader import AgentPersonaLoader
from src.types import ResearchMetadata, SynthesizedResult
from src.pipeline.pipeline import PipelineContext, PipelineStage

logger = logging.getLogger(__name__)


class ReportStage(PipelineStage):
    """Stage independente para geração de relatórios com paralelização e cache.

    Este stage consolida a geração de 3 seções principais (executive summary,
    recommendation e trends) em uma única chamada LLM com schema JSON estruturado,
    executando em paralelo com outros stages e utilizando cache para seções
    reutilizáveis.

    Attributes:
        llm_client: Cliente LLM para geração de conteúdo.
        cache: Instância do SharedCache para cache de seções.
        temporal_analyzer: Analisador temporal para timeline.
        sentiment_analyzer: Analisador de sentimento.
        comparator: Comparador de alternativas.
    """

    name = "report"
    critical = True

    # Schema JSON para a chamada LLM consolidada
    REPORT_SCHEMA = {
        "type": "object",
        "properties": {
            "executive_summary": {
                "type": "string",
                "description": "Resumo executivo de 3-5 frases sobre os achados principais",
            },
            "recommendation": {
                "type": "string",
                "description": "Recomendação estratégica com estrutura: 1) Recomendação principal, 2) Alternativa, 3) Próximos passos",
            },
            "trends": {
                "type": "string",
                "description": "2-3 tendências tecnológicas identificadas com evidências concretas",
            },
        },
        "required": ["executive_summary", "recommendation", "trends"],
    }

    def __init__(
        self,
        orchestrator_or_llm: Any = None,
        cache: SharedCache | None = None,
        llm_client: Any = None,
    ):
        """Inicializa o ReportStage.

        Suporta tanto a instanciação via factory do pipeline (passando o orchestrator)
        quanto instanciação direta para testes (passando o llm_client).

        Args:
            orchestrator_or_llm: Instância do Orchestrator ou do LLMClient.
            cache: Instância do SharedCache. Se None, cria/reutiliza.
            llm_client: Nome alternativo para o LLMClient para compatibilidade.
        """
        # Detecção de tipo para compatibilidade com Pipeline vs Unit Test
        target_llm = llm_client or orchestrator_or_llm
        from unittest.mock import Mock

        if hasattr(target_llm, "llm") and not isinstance(target_llm, Mock):
            self.orchestrator = target_llm
            self._llm = target_llm.llm
            self.cache = (
                getattr(target_llm, "smart_cache", None) or cache or SharedCache()
            )
        else:
            self.orchestrator = None
            self._llm = target_llm
            self.cache = cache or SharedCache()

        # Importa analisadores especializados
        from src.temporal_analyzer import TemporalAnalyzer
        from src.sentiment_analyzer import SentimentAnalyzer
        from src.comparator import Comparator

        self.temporal_analyzer = TemporalAnalyzer()
        self.sentiment_analyzer = SentimentAnalyzer()
        self.comparator = Comparator()

        # Persona loader for agent injection
        self.persona_loader = AgentPersonaLoader()

    @property
    def llm(self) -> Any:
        if self.orchestrator is not None:
            return self.orchestrator.llm
        return self._llm

    @llm.setter
    def llm(self, value: Any) -> None:
        self._llm = value

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Método de entrada do pipeline que executa a geração do relatório.

        Lê os resultados sintetizados do contexto, executa o stage e
        armazena o relatório em markdown no `context.report`.
        """
        query = context.query
        results = context.synthesized_results or []

        # Extrai ou constrói o ResearchMetadata
        metadata = context.metadata
        if not isinstance(metadata, ResearchMetadata):
            # Fallback seguro para metadados
            metadata = ResearchMetadata(
                query=query,
                timestamp=datetime.now(),
                domain=context.get("domain", "general"),
                sources=list(context.completed_stages),
                total_results=len(results),
                iterations=context.get("iterations", 1),
                overall_confidence=context.get("overall_confidence", 0.8),
                low_confidence_warnings=context.get("low_confidence_warnings", []),
            )

        # Executa geração das seções
        sections = await self.execute(query, results, metadata)

        # Monta relatório final em Markdown
        report_md = self.assemble_report(query, metadata, results, sections)

        # Seção de arquiteturas de repositórios (gerada pelo VerificationStage com Scout)
        repo_architectures = context.extra.get("repo_architectures", []) if hasattr(context, "extra") else []
        if repo_architectures:
            arch_section = "\n## 🔍 Mapa de Arquitetura dos Concorrentes (Scout)\n\n"
            for repo in repo_architectures:
                arch_section += f"### {repo.get('url', 'Repositório')}\n\n"
                arch_section += repo.get("architecture_map", "_Não disponível._") + "\n\n"
            report_md += arch_section

        # Salva resultados no contexto do pipeline
        context.report = report_md
        context.set("report_sections", sections)

        return context

    async def execute(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> dict[str, str]:
        """Executa a lógica de geração de relatório.

        Este método paraleliza a geração das 3 seções LLM (executive summary,
        recommendation e trends) e consolida em uma única chamada com schema JSON.
        Seções reutilizáveis são cacheadas para otimização.

        Args:
            query: Query original do usuário.
            results: Lista de SynthesizedResult ordenada por score.
            metadata: Metadados da sessão de pesquisa.

        Returns:
            dict[str, str]: Dicionário com as seções geradas:
                - executive_summary: Resumo executivo
                - recommendation: Recomendação estratégica
                - trends: Tendências tecnológicas
                - timeline_section: Seção de timeline cronológica
                - sentiment_section: Seção de análise de sentimento
                - comparison_section: Seção de comparação de alternativas
        """
        logger.info(
            f"ReportStage: Iniciando geração de relatório para query: {query[:50]}..."
        )

        # Verifica cache para seções reutilizáveis
        cache_key = self._make_cache_key(query, results)
        cached_sections = await self._get_cached_sections(cache_key)

        if cached_sections:
            logger.info("ReportStage: Usando seções cacheadas para otimização")
            return cached_sections

        # Paraleliza as 3 seções LLM
        llm_sections = await self._generate_llm_sections_parallel(
            query, results, metadata
        )

        # Gera seções determinísticas (não dependem de LLM)
        timeline_section = self.temporal_analyzer.generate_timeline_section(results)
        sentiment_section = self.sentiment_analyzer.generate_sentiment_section(results)
        comparison_section = self.comparator.generate_comparison_section(query, results)

        # Consolida todas as seções
        all_sections = {
            **llm_sections,
            "timeline_section": timeline_section,
            "sentiment_section": sentiment_section,
            "comparison_section": comparison_section,
        }

        # Cacheia as seções para reuso futuro
        await self._cache_sections(cache_key, all_sections)

        logger.info("ReportStage: Geração de relatório concluída com sucesso")
        return all_sections

    async def _generate_llm_sections_parallel(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> dict[str, str]:
        """Gera as 3 seções LLM em paralelo com consolidação em 1 chamada.

        Tenta primeiro uma chamada LLM consolidada com schema JSON.
        Se falhar, faz fallback para chamadas individuais paralelas.

        Args:
            query: Query original do usuário.
            results: Lista de SynthesizedResult.
            metadata: Metadados da pesquisa.

        Returns:
            dict[str, str]: Dicionário com as 3 seções LLM geradas.
        """
        # Tenta chamada consolidada primeiro
        try:
            consolidated = await self._generate_consolidated_llm_call(
                query, results, metadata
            )
            if consolidated:
                logger.info("ReportStage: Chamada LLM consolidada bem-sucedida")
                return consolidated
        except Exception as e:
            logger.warning(
                f"ReportStage: Chamada consolidada falhou, usando fallback paralelo: {e}"
            )

        # Fallback: chamadas individuais em paralelo
        return await self._generate_individual_sections_parallel(
            query, results, metadata
        )

    async def _generate_consolidated_llm_call(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> dict[str, str] | None:
        """Faz uma única chamada LLM para gerar as 3 seções com schema JSON.

        Args:
            query: Query original do usuário.
            results: Lista de SynthesizedResult.
            metadata: Metadados da pesquisa.

        Returns:
            dict[str, str] | None: Dicionário com as 3 seções ou None se falhar.
        """
        prompt = self._build_consolidated_prompt(query, results, metadata)

        try:
            # Usa o método complete com task_type para roteamento inteligente
            response = await self.llm.complete(
                prompt=prompt,
                task_type="report_generation",
                temperature=0.4,
                max_tokens=2000,
            )

            # Tenta fazer parse do JSON
            sections = self._parse_json_response(response)
            if sections and self._validate_sections(sections):
                return sections

            logger.warning("ReportStage: Resposta JSON inválida da chamada consolidada")
            return None

        except Exception as e:
            logger.error(f"ReportStage: Erro na chamada consolidada: {e}")
            return None

    async def _generate_individual_sections_parallel(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> dict[str, str]:
        """Gera as 3 seções individualmente em paralelo (fallback).

        Args:
            query: Query original do usuário.
            results: Lista de SynthesizedResult.
            metadata: Metadados da pesquisa.

        Returns:
            dict[str, str]: Dicionário com as 3 seções LLM geradas.
        """
        # Cria tasks para execução paralela
        tasks = [
            self._generate_executive_summary(query, results, metadata),
            self._generate_recommendation(query, results),
            self._generate_trends(results),
        ]

        # Executa em paralelo
        executive_summary, recommendation, trends = await asyncio.gather(
            *tasks, return_exceptions=True
        )

        # Trata exceções
        if isinstance(executive_summary, Exception):
            logger.warning(
                f"ReportStage: executive_summary falhou: {executive_summary}"
            )
            executive_summary = self._fallback_executive_summary(
                query, results, metadata
            )

        if isinstance(recommendation, Exception):
            logger.warning(f"ReportStage: recommendation falhou: {recommendation}")
            recommendation = self._fallback_recommendation(results)

        if isinstance(trends, Exception):
            logger.warning(f"ReportStage: trends falhou: {trends}")
            trends = self._fallback_trends(results)

        return {
            "executive_summary": executive_summary,
            "recommendation": recommendation,
            "trends": trends,
        }

    def _build_consolidated_prompt(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> str:
        """Constrói o prompt consolidado para as 3 seções.

        Args:
            query: Query original do usuário.
            results: Lista de SynthesizedResult.
            metadata: Metadados da pesquisa.

        Returns:
            str: Prompt formatado para a chamada LLM consolidada.
        """
        # Prepara contexto dos top resultados
        top_lines = self._format_top_results(results[:5])

        # Metadados de confiança
        confidence_note = (
            f"Confiança geral da pesquisa: {metadata.overall_confidence:.0%}"
            if metadata.overall_confidence > 0
            else ""
        )
        warnings_note = (
            f"Advertências: {'; '.join(w for w in metadata.low_confidence_warnings[:3] if w)}"
            if metadata.low_confidence_warnings
            else ""
        )

        prompt = f"""Você é um analista técnico sênior. Escreva em Português do Brasil.

Gere UM ÚNICO JSON com exatamente 3 campos: "executive_summary", "recommendation" e "trends".

REGRAS GERAIS:
- Use dados concretos (stars, datas, linguagens) quando disponíveis.
- Admita limitações quando a confiança for baixa. Não invente informações.
- Priorize fontes marcadas com [ALTA CONFIANÇA].

CONTEXTO DA PESQUISA:
- Query: {query}
- Domínio: {metadata.domain}
- Fontes pesquisadas: {', '.join(s for s in metadata.sources if s)}
- Resultados encontrados: {metadata.total_results}
- Iterações: {metadata.iterations}
{confidence_note}
{warnings_note}

TOP 5 PROJETOS ENCONTRADOS:
{top_lines}

INSTRUÇÕES ESPECÍFICAS POR SEÇÃO:

1. executive_summary (3-5 frases):
   - Resuma os achados principais da pesquisa.
   - Cite dados concretos dos projetos encontrados.

2. recommendation (estrutura obrigatória):
   - **Recomendação principal**: qual projeto e POR QUÊ (cite um dado concreto)
   - **Alternativa**: segundo melhor e quando escolhê-la
   - **Próximos passos**: máximo 3 ações específicas e acionáveis

3. trends (2-3 tendências):
   - Cada tendência DEVE citar pelo menos um projeto concreto como evidência.
   - Não extrapole além dos dados.

RESPONDA APENAS COM O JSON VÁLIDO, sem texto adicional:
{{"executive_summary": "...", "recommendation": "...", "trends": "..."}}
"""

        # Injeta Sage se o modo for estratégico e custo não for otimizado
        if self.orchestrator:
            op_config = getattr(self.orchestrator, "operation_config", None)
            if op_config:
                op_name = getattr(op_config, "name", "")
                cost_opt = getattr(op_config, "cost_optimization", False)
                if not cost_opt and op_name in ("concorrencia", "radar", "black_ops"):
                    prompt = self.persona_loader.build_enhanced_prompt(prompt, "sage_strategy")
                    logger.info("ReportStage: persona Sage injetada para modo '%s'.", op_name)

        return prompt

    def _parse_json_response(self, response: str) -> dict[str, str] | None:
        """Faz parse da resposta JSON do LLM.

        Args:
            response: Resposta bruta do LLM.

        Returns:
            dict[str, str] | None: Dicionário parseado ou None se inválido.
        """
        try:
            # Tenta extrair JSON da resposta
            # Remove markdown code blocks se presentes
            response = response.strip()
            if response.startswith("```"):
                # Remove ```json e ```
                lines = response.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].strip() == "```":
                    lines = lines[:-1]
                response = "\n".join(lines)

            parsed = json.loads(response)

            # Valida campos obrigatórios
            required = ["executive_summary", "recommendation", "trends"]
            if all(k in parsed for k in required):
                return {
                    "executive_summary": str(parsed["executive_summary"]),
                    "recommendation": str(parsed["recommendation"]),
                    "trends": str(parsed["trends"]),
                }

            return None

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"ReportStage: Falha no parse JSON: {e}")
            return None

    def _validate_sections(self, sections: dict[str, str]) -> bool:
        """Valida se as seções geradas têm conteúdo mínimo.

        Args:
            sections: Dicionário com as seções.

        Returns:
            bool: True se todas as seções são válidas.
        """
        for key, value in sections.items():
            if not value or len(value.strip()) < 50:
                logger.warning(f"ReportStage: Seção '{key}' muito curta ou vazia")
                return False
        return True

    async def _generate_executive_summary(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> str:
        """Gera o resumo executivo da pesquisa usando o LLM.

        Args:
            query: Query original do usuário.
            results: Resultados sintetizados para contexto do LLM.
            metadata: Metadados da pesquisa.

        Returns:
            str: Parágrafos de resumo executivo gerados pelo LLM.
        """
        top_lines = self._format_top_results(results[:5])

        confidence_note = (
            f"Confiança geral da pesquisa: {metadata.overall_confidence:.0%}"
            if metadata.overall_confidence > 0
            else ""
        )
        warnings_note = (
            f"Advertências: {'; '.join(w for w in metadata.low_confidence_warnings[:3] if w)}"
            if metadata.low_confidence_warnings
            else ""
        )

        prompt = (
            "Você é um analista técnico sênior. Escreva em Português do Brasil.\n"
            "Gere um resumo executivo de 3-5 frases sobre os achados principais.\n\n"
            "Regras: use dados concretos (stars, datas, linguagens) quando disponíveis.\n"
            "Admita limitações quando a confiança for baixa. Não invente informações.\n"
            "Priorize fontes marcadas com [ALTA CONFIANÇA] e descarte ou mencione com cautela fontes marcadas com [BAIXA — VERIFICAR].\n\n"
            f"Query: {query}\n"
            f"Domínio: {metadata.domain}\n"
            f"Fontes pesquisadas: {', '.join(s for s in metadata.sources if s)}\n"
            f"Resultados encontrados: {metadata.total_results}\n"
            f"Iterações: {metadata.iterations}\n"
            f"{confidence_note}\n"
            f"{warnings_note}\n\n"
            f"Top 5 projetos encontrados:\n{top_lines}\n\n"
            "Resumo executivo:"
        )

        try:
            return await self.llm.generate(prompt, temperature=0.4, max_tokens=500)
        except Exception as e:
            logger.warning(f"ReportStage: LLM executive summary falhou: {e}")
            return self._fallback_executive_summary(query, results, metadata)

    async def _generate_recommendation(
        self,
        query: str,
        results: list[SynthesizedResult],
    ) -> str:
        """Gera uma recomendação estratégica baseada nos resultados da pesquisa.

        Args:
            query: Query original do usuário.
            results: Resultados sintetizados para contexto do LLM.

        Returns:
            str: Recomendação acionável gerada pelo LLM.
        """
        if not results:
            return "Nenhum projeto encontrado para recomendação."

        top_lines = self._format_top_results_with_highlights(results[:5])

        prompt = (
            "Você é um consultor técnico. Escreva em Português do Brasil.\n"
            "Baseado nos projetos encontrados, dê uma recomendação final clara e rastreável.\n\n"
            "Estrutura obrigatória:\n"
            "1. **Recomendação principal** — qual projeto e POR QUÊ (cite um dado concreto)\n"
            "2. **Alternativa** — segundo melhor e quando escolhê-la\n"
            "3. **Próximos passos** — máximo 3 ações específicas e acionáveis\n\n"
            "Regras: baseie cada afirmação nos dados abaixo. Não extrapole além dos dados.\n"
            "Dê preferência clara aos projetos marcados com [ALTA CONFIANÇA]. Evite recomendar itens [BAIXA — VERIFICAR] como opção primária.\n\n"
            f"Query do usuário: {query}\n\n"
            f"Projetos (ordenados por relevância):\n{top_lines}\n\n"
            "Recomendação final:"
        )

        try:
            return await self.llm.generate(prompt, temperature=0.3, max_tokens=800)
        except Exception as e:
            logger.warning(f"ReportStage: LLM recommendation falhou: {e}")
            return self._fallback_recommendation(results)

    async def _generate_trends(self, results: list[SynthesizedResult]) -> str:
        """Identifica tendências tecnológicas a partir dos resultados de pesquisa.

        Args:
            results: Resultados sintetizados com títulos e descrições dos projetos.

        Returns:
            str: Bloco de tendências em Markdown gerado pelo LLM.
        """
        if len(results) < 3:
            return "Poucos dados para análise de tendências."

        project_lines = "\n".join(
            f"- {r.title or '(sem título)'}: {(r.description or '')[:150]}..."
            for r in results[:8]
        )

        prompt = (
            "Analise os projetos encontrados e identifique 2-3 tendências tecnológicas.\n\n"
            "Regras: cada tendência DEVE citar pelo menos um projeto concreto como evidência.\n"
            "Não extrapole além dos dados. Se os dados forem insuficientes, diga isso.\n\n"
            f"Projetos:\n{project_lines}\n\n"
            "Tendências observadas (em Português do Brasil):"
        )

        try:
            return await self.llm.generate(prompt, temperature=0.4, max_tokens=400)
        except Exception as e:
            logger.warning(f"ReportStage: LLM trends falhou: {e}")
            return self._fallback_trends(results)

    def _format_top_results(self, results: list[SynthesizedResult]) -> str:
        """Formata os top resultados para o prompt LLM.

        Args:
            results: Lista de SynthesizedResult.

        Returns:
            str: Texto formatado com os resultados.
        """
        lines = []
        for i, r in enumerate(results):
            quality = getattr(r, "evidence_quality", "unknown")
            confidence_tag = (
                "[ALTA CONFIANÇA]"
                if quality == "verified"
                else "[MÉDIA]"
                if quality == "cited"
                else "[BAIXA — VERIFICAR]"
            )
            lines.append(
                f"{i+1}. {confidence_tag} {r.title or '(sem título)'} "
                f"({', '.join(s for s in r.sources if s)}) - score: {r.combined_score}\n"
                f"   {(r.description or '')[:200]}..."
            )
        return "\n".join(lines)

    def _format_top_results_with_highlights(
        self, results: list[SynthesizedResult]
    ) -> str:
        """Formata os top resultados com highlights para o prompt de recomendação.

        Args:
            results: Lista de SynthesizedResult.

        Returns:
            str: Texto formatado com os resultados e highlights.
        """
        lines = []
        for i, r in enumerate(results):
            quality = getattr(r, "evidence_quality", "unknown")
            confidence_tag = (
                "[ALTA CONFIANÇA]"
                if quality == "verified"
                else "[MÉDIA]"
                if quality == "cited"
                else "[BAIXA — VERIFICAR]"
            )
            lines.append(
                f"{i+1}. {confidence_tag} {r.title or '(sem título)'}\n"
                f"   Pontos fortes: {', '.join(h for h in r.highlights if h)}\n"
                f"   Métricas: {r.metrics}"
            )
        return "\n".join(lines)

    def _fallback_executive_summary(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> str:
        """Fallback textual para o resumo executivo.

        Args:
            query: Query original.
            results: Resultados da pesquisa.
            metadata: Metadados da pesquisa.

        Returns:
            str: Resumo executivo gerado textualmente.
        """
        return (
            f"Pesquisa sobre '{query}' encontrou {len(results)} projetos relevantes "
            f"em {', '.join(s for s in metadata.sources if s)}."
        )

    def _fallback_recommendation(self, results: list[SynthesizedResult]) -> str:
        """Fallback textual para a recomendação.

        Args:
            results: Resultados da pesquisa.

        Returns:
            str: Recomendação gerada textualmente.
        """
        if not results:
            return "Nenhum projeto encontrado para recomendação."
        top = results[0]
        return f"Recomendamos **{top.title}** como principal opção. {top.description[:200]}..."

    def _fallback_trends(self, results: list[SynthesizedResult]) -> str:
        """Fallback textual para tendências.

        Args:
            results: Resultados da pesquisa.

        Returns:
            str: Texto de tendências gerado textualmente.
        """
        if len(results) < 3:
            return "Poucos dados para análise de tendências."
        return "Análise de tendências não disponível."

    def _make_cache_key(self, query: str, results: list[SynthesizedResult]) -> str:
        """Gera uma chave de cache determinística baseada na query e resultados.

        Args:
            query: Query original.
            results: Lista de resultados.

        Returns:
            str: Chave de cache SHA-256.
        """
        # Usa query + top 5 entity IDs para criar chave única
        result_ids = [r.entity for r in results[:5]]
        cache_input = f"{query.lower().strip()}:{'|'.join(result_ids)}"
        return hashlib.sha256(cache_input.encode()).hexdigest()[:24]

    async def _get_cached_sections(self, cache_key: str) -> dict[str, str] | None:
        """Recupera seções cacheadas se disponíveis.

        Args:
            cache_key: Chave de cache.

        Returns:
            dict[str, str] | None: Seções cacheadas ou None.
        """
        try:
            cached = await self.cache.get(f"report_stage:{cache_key}")
            if cached and isinstance(cached, dict):
                logger.debug(f"ReportStage: Cache HIT para key {cache_key}")
                return cached
        except Exception as e:
            logger.debug(f"ReportStage: Erro ao ler cache: {e}")
        return None

    async def _cache_sections(self, cache_key: str, sections: dict[str, str]) -> None:
        """Cacheia as seções geradas para reuso futuro.

        Args:
            cache_key: Chave de cache.
            sections: Seções a serem cacheadas.
        """
        try:
            # Usa estratégia "moderate" (48h) para seções de relatório
            await self.cache.set(
                f"report_stage:{cache_key}",
                sections,
                strategy="moderate",
            )
            logger.debug(f"ReportStage: Seções cacheadas com key {cache_key}")
        except Exception as e:
            logger.warning(f"ReportStage: Erro ao escrever cache: {e}")

    def assemble_report(
        self,
        query: str,
        metadata: ResearchMetadata,
        results: list[SynthesizedResult],
        sections: dict[str, str],
    ) -> str:
        """Monta o relatório final unindo todas as seções geradas.

        Este método é compatível com o ReportGenerator original e pode ser
        usado para integrar com o pipeline existente.

        Args:
            query: Query original do usuário.
            metadata: Metadados da pesquisa.
            results: Resultados sintetizados.
            sections: Dicionário com todas as seções geradas.

        Returns:
            str: Relatório completo em formato Markdown.
        """
        from src.report_generator import ReportGenerator

        # Cria uma instância temporária do ReportGenerator apenas para montagem
        # (sem o LLM, pois as seções já foram geradas)
        generator = ReportGenerator.__new__(ReportGenerator)
        generator.temporal_analyzer = self.temporal_analyzer
        generator.sentiment_analyzer = self.sentiment_analyzer
        generator.comparator = self.comparator

        # Chama o método de montagem
        return generator._assemble_report(
            query=query,
            metadata=metadata,
            results=results,
            executive_summary=sections.get("executive_summary"),
            recommendation=sections.get("recommendation"),
            trends=sections.get("trends"),
            timeline_section=sections.get("timeline_section", ""),
            sentiment_section=sections.get("sentiment_section", ""),
            comparison_section=sections.get("comparison_section", ""),
        )


# ── Factory Function ─────────────────────────────────────────────────────────


def create_report_stage(
    llm_client: Any, cache: SharedCache | None = None
) -> ReportStage:
    """Factory function para criar uma instância do ReportStage.

    Args:
        llm_client: Cliente LLM para geração de conteúdo.
        cache: Instância do SharedCache. Se None, cria uma nova instância.

    Returns:
        ReportStage: Instância configurada do stage.
    """
    return ReportStage(llm_client=llm_client, cache=cache)
