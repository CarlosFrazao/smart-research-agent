"""Módulo de geração de relatórios de pesquisa em Markdown.
Orquestra a montagem de relatórios estruturados a partir de resultados sintetizados,
combinando sumário executivo gerado por LLM, análise de fontes, tendências,
sentimento, comparações e timeline cronológica.
"""

import asyncio
import hashlib
import logging
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from src.cache import Cache
from src.clients.llm_client import LLMClient
from src.comparator import Comparator
from src.sentiment_analyzer import SentimentAnalyzer
from src.temporal_analyzer import TemporalAnalyzer
from src.types import (
    Domain,
    ResearchMetadata,
    ReportFormat,
    SearchResult,
    SynthesizedResult,
)
from typing import Any

logger = logging.getLogger(__name__)

# TTL do cache de secoes LLM (resumo/recomendacao/tendencias) do relatorio.
# Cobre reexecucoes/reexportacoes (md+pdf+docx) do mesmo conjunto de resultados
# dentro de uma mesma sessao de pesquisa, sem gastar chamadas de LLM extras.
_SECTIONS_CACHE_TTL_SECONDS = 1800  # 30 minutos


class ReportGenerator:
    """Gerador de relatórios Markdown estruturados a partir de resultados de pesquisa.
    Combina sumário executivo, análise comparativa, timeline, sentimento e
    tendências (via LLM) em um único documento Markdown coeso e exportável.
    """

    def __init__(self, llm_client: LLMClient, cache: Cache | None = None):
        self.llm = llm_client
        self.temporal_analyzer = TemporalAnalyzer()
        self.sentiment_analyzer = SentimentAnalyzer()
        self.comparator = Comparator()
        # Cache das 3 secoes narrativas (resumo/recomendacao/tendencias).
        self.cache = cache or Cache()

    def _get_confidence_tags(self, is_english: bool) -> dict:
        """Retorna as tags de confiança traduzidas para usar nos prompts e relatórios."""
        if is_english:
            return {
                "verified": "[HIGH CONFIDENCE]",
                "cited": "[MEDIUM]",
                "default": "[LOW — VERIFY]",
            }
        return {
            "verified": "[ALTA CONFIANÇA]",
            "cited": "[MÉDIA]",
            "default": "[BAIXA — VERIFICAR]",
        }

    def _is_query_english(self, query: str) -> bool:
        """Heurística simples para detectar se a query do usuário está em inglês."""
        en_words = {
            "vs",
            "how",
            "what",
            "best",
            "comparison",
            "tool",
            "library",
            "alternative",
            "framework",
            "open-source",
            "open source",
            "client",
            "server",
            "api",
            "vs.",
            "compare",
            "why",
            "with",
            "benchmark",
            "versus",
        }
        pt_words = {
            "como",
            "qual",
            "melhor",
            "comparacao",
            "ferramenta",
            "biblioteca",
            "alternativa",
            "para",
            "com",
            "por",
            "que",
            "uma",
            "um",
            "sobre",
            "entre",
        }
        words = set(query.lower().split())
        has_en = bool(words & en_words)
        has_pt = bool(words & pt_words)

        # Se contém palavras em português, consideramos português (mesmo que tenha "vs")
        if has_pt:
            return False
        # Se contém palavras em inglês e não tem em português, consideramos inglês
        if has_en:
            return True
        return False

    def _translate_analyzer_sections(self, section: str, is_english: bool) -> str:
        """Traduz os cabeçalhos das seções geradas por SentimentAnalyzer e TemporalAnalyzer.
        Como esses módulos ainda não suportam inglês nativamente, fazemos uma substituição
        de strings para garantir consistência no relatório final.
        """
        if not is_english:
            return section

        # Traduções para SentimentAnalyzer
        section = section.replace(
            "## 🎭 Análise de Sentimento & Viés", "## 🎭 Sentiment Analysis & Bias"
        )
        section = section.replace(
            "### 📊 Perfil de Sentimento por Canal de Origem",
            "### 📊 Sentiment Profile by Source Channel",
        )
        section = section.replace("Canal de Origem", "Source Channel")
        section = section.replace("Relevância / Volume", "Relevance / Volume")
        section = section.replace("Tom Médio / Sentimento", "Average Tone / Sentiment")
        section = section.replace("Classificação", "Classification")
        section = section.replace(
            "Nenhum dado disponível para análise de sentimento.",
            "No data available for sentiment analysis.",
        )

        # Traduções para TemporalAnalyzer
        section = section.replace(
            "## 📅 Linha do Tempo & Análise Temporal",
            "## 📅 Timeline & Temporal Analysis",
        )
        section = section.replace(
            "### 📊 Histograma de Menções/Atividade por Ano",
            "### 📊 Mentions/Activity Histogram by Year",
        )
        section = section.replace("Ano", "Year")
        section = section.replace("Ocorrências / Eventos", "Occurrences / Events")
        section = section.replace("Histórico Visual", "Visual History")
        section = section.replace("Análise de Tendência:", "Trend Analysis:")
        section = section.replace(
            "📈 **Tendência Crescente (Alta de Interesse / Atividade)**",
            "📈 **Rising Trend (Increased Interest / Activity)**",
        )
        section = section.replace(
            "📉 **Tendência Decrescente (Queda de Interesse / Atividade)**",
            "📉 **Declining Trend (Decreased Interest / Activity)**",
        )
        section = section.replace(
            "➡️ **Tendência Estável / Consolidada**", "➡️ **Stable / Consolidated Trend**"
        )
        section = section.replace(
            "❓ **Dados Temporais Insuficientes para Análise de Tendência**",
            "❓ **Insufficient Temporal Data for Trend Analysis**",
        )
        section = section.replace(
            "Nenhuma informação temporal significativa pôde ser extraída dos dados coletados.",
            "No significant temporal information could be extracted from the collected data.",
        )

        return section

    async def generate(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> str:
        """Gera o relatório completo de pesquisa como string Markdown."""
        if not results:
            is_english = self._is_query_english(query)
            if is_english:
                return f"# Research Report: {query}\n\nNo relevant results were found in the searched sources."
            else:
                return f"# Relatório de Pesquisa: {query}\n\nNenhum resultado relevante foi encontrado nas fontes pesquisadas."

        sections = await self._generate_report_sections(query, results, metadata)
        executive_summary = sections["executive_summary"]
        recommendation = sections["recommendation"]
        trends = sections["trends"]

        timeline_section = self.temporal_analyzer.generate_timeline_section(results)
        sentiment_section = self.sentiment_analyzer.generate_sentiment_section(results)

        # Integração crítica: usa o Comparator para gerar tabela rica se a query for comparativa
        comparison_section = self.comparator.generate_comparison_section(query, results)

        # FASE 5 — Perspectivas por tom (GDELT): monta o espectro de como
        # diferentes fontes cobriram o mesmo evento com base no tone.
        perspectives_section = self._build_perspectives_section(query, results)

        report_raw = self._assemble_report(
            query=query,
            metadata=metadata,
            results=results,
            executive_summary=executive_summary,
            recommendation=recommendation,
            trends=trends,
            timeline_section=timeline_section,
            sentiment_section=sentiment_section,
            comparison_section=comparison_section,
            perspectives_section=perspectives_section,
        )
        return await self._validate_and_enrich_sections(report_raw, query, results)

    async def _validate_and_enrich_sections(
        self, report_md: str, query: str, results: list[SynthesizedResult]
    ) -> str:
        """Valida se as seções do relatório gerado são muito curtas ou vazias e enriquece se necessário."""
        is_english = self._is_query_english(query)
        sections = report_md.split("\n## ")
        enriched_sections = []

        # A primeira parte do split é o título e metadados
        enriched_sections.append(sections[0])

        for section in sections[1:]:
            lines = section.split("\n")
            header = lines[0]
            content = "\n".join(lines[1:]).strip()

            # Se a seção (excluindo links/linhas vazias) tiver menos de 200 caracteres úteis
            clean_content = content.replace("---", "").strip()
            if len(clean_content) < 200:
                logger.info(
                    f"ReportGenerator: Seção '{header}' curta demais ({len(clean_content)} chars). Enriquecendo via LLM..."
                )
                project_summaries = "\n".join(
                    f"- {r.title}: {(r.description or '')[:200]}" for r in results[:8]
                )

                if is_english:
                    prompt = (
                        "You are a senior technology intelligence analyst. Write in English.\n"
                        f"The section '{header}' of a technical report on '{query}' is empty or too short.\n"
                        "Generate a detailed, in-depth, and formal technical analysis (minimum of 3 robust paragraphs) for this section.\n\n"
                        f"Found projects as context:\n{project_summaries}\n\n"
                        f"Specific guidelines for section '{header}':\n"
                    )
                    if "Technologies" in header or "Stacks" in header:
                        prompt += (
                            "- Identify likely languages (e.g. Rust, Python, TypeScript) and why they are used.\n"
                            "- Elaborate on the transport architecture (HTTP, WebSockets, stdio) commonly employed.\n"
                            "- Detail dependencies and ecosystems involved."
                        )
                    elif "Community" in header or "Discussion" in header:
                        prompt += (
                            "- Synthesize the overall reception of this type of technology by the developer community.\n"
                            "- Discuss main bottlenecks discussed (learning curve, data security in LLMs).\n"
                        )
                    elif "Sentiment" in header:
                        prompt += (
                            "- Describe the general tone of mentions (optimistic, pragmatic, skeptical).\n"
                            "- Point out reasons for enthusiasm and sources of skepticism."
                        )
                    else:
                        prompt += (
                            "- Elaborate and deepen technical conclusions based on provided data.\n"
                            "- Discuss market implications and medium-term adoption."
                        )
                else:
                    prompt = (
                        "Você é um analista sênior de inteligência tecnológica. Escreva em Português do Brasil.\n"
                        f"A seção '{header}' de um relatório técnico sobre '{query}' está vazia ou curta demais.\n"
                        "Gere uma análise técnica detalhada, aprofundada e formal (mínimo de 3 parágrafos robustos) para esta seção.\n\n"
                        f"Projetos encontrados como contexto:\n{project_summaries}\n\n"
                        f"Diretrizes específicas para a seção '{header}':\n"
                    )
                    if "Tecnologias" in header or "Stacks" in header:
                        prompt += (
                            "- Identifique as linguagens prováveis (ex: Rust, Python, TypeScript) e por que são usadas.\n"
                            "- Discorra sobre a arquitetura de transporte (HTTP, WebSockets, stdio) comumente empregada.\n"
                        )
                    elif (
                        "Discussao" in header
                        or "Discussão" in header
                        or "Comunidade" in header
                    ):
                        prompt += (
                            "- Sintetize a recepção geral desse tipo de tecnologia pela comunidade de desenvolvedores.\n"
                            "- Fale sobre os principais gargalos discutidos (curva de aprendizado, segurança de dados em LLMs).\n"
                        )
                    elif "Sentimento" in header:
                        prompt += (
                            "- Descreva o tom geral das menções (otimista, pragmático, cético).\n"
                            "- Aponte os motivos do entusiasmo e as fontes de ceticismo."
                        )
                    else:
                        prompt += (
                            "- Elabore e aprofunde as conclusões técnicas baseadas nos dados fornecidos.\n"
                            "- Discorra sobre implicações de mercado e adoção a médio prazo."
                        )

                try:
                    enriched_content = await self.llm.generate(
                        prompt, temperature=0.5, max_tokens=600
                    )
                    if enriched_content and len(enriched_content) > 100:
                        section = f"{header}\n\n{enriched_content}\n"
                except Exception as e:
                    section = f"{header}\n\n" + (
                        "No additional details could be loaded for this section."
                        if is_english
                        else "Não foi possível carregar detalhes adicionais para esta seção."
                    )
                    logger.warning(f"Falha ao enriquecer seção '{header}': {e}")

            enriched_sections.append(section)

        return "\n## ".join(enriched_sections)

    def _sections_cache_key(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> str:
        """Gera uma chave de cache determinística para as 3 secoes narrativas."""
        fingerprint_parts = [
            query.strip().lower(),
            metadata.domain,
            str(metadata.total_results),
        ]
        for r in results[:8]:
            quality = getattr(r, "evidence_quality", "unknown")
            fingerprint_parts.append(
                f"{r.title}|{r.combined_score}|{quality}|{','.join(sorted(s for s in r.sources if s))}"
            )
        fingerprint = "||".join(fingerprint_parts)
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return f"report_sections:{digest}"

    async def _generate_report_sections(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> dict[str, str]:
        """Obtem as 3 secoes narrativas do relatorio (resumo, recomendacao, tendencias)."""

        async def _parallel_fallback() -> dict[str, str]:
            executive_summary, recommendation, trends = await asyncio.gather(
                self._generate_executive_summary(query, results, metadata),
                self._generate_recommendation(query, results),
                self._generate_trends(results, query=query),
            )
            return {
                "executive_summary": executive_summary,
                "recommendation": recommendation,
                "trends": trends,
            }

        if not results or len(results) < 3:
            return await _parallel_fallback()

        cache_key = self._sections_cache_key(query, results, metadata)
        try:
            cached = await self.cache.get(cache_key)
        except Exception as e:
            logger.warning(f"ReportGenerator: falha ao consultar cache de secoes: {e}")
            cached = None

        if cached and all(
            cached.get(k) for k in ("executive_summary", "recommendation", "trends")
        ):
            logger.info("ReportGenerator: secoes narrativas recuperadas do cache.")
            return cached

        try:
            sections = await self._generate_sections_consolidated(
                query, results, metadata
            )
        except Exception as e:
            logger.warning(
                f"ReportGenerator: chamada consolidada falhou ({e}); "
                "usando fallback paralelo com prompts individuais."
            )
            sections = await _parallel_fallback()

        try:
            await self.cache.set(
                cache_key,
                sections,
                ttl_seconds=_SECTIONS_CACHE_TTL_SECONDS,
                source_type="report_sections",
            )
        except Exception as e:
            logger.warning(f"ReportGenerator: falha ao gravar cache de secoes: {e}")

        return sections

    async def _generate_sections_consolidated(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> dict[str, str]:
        """Gera resumo executivo, recomendacao e tendencias em 1 unica chamada LLM."""
        is_english = self._is_query_english(query)
        tags = self._get_confidence_tags(is_english)
        high_tag = tags["verified"]
        low_tag = tags["default"]

        top_lines_list = []
        for i, r in enumerate(results[:8]):
            quality = getattr(r, "evidence_quality", "unknown")
            confidence_tag = (
                tags["verified"]
                if quality == "verified"
                else tags["cited"]
                if quality == "cited"
                else tags["default"]
            )

            if is_english:
                desc_label, highlights_label, metrics_label = (
                    "Description",
                    "Highlights",
                    "Metrics",
                )
            else:
                desc_label, highlights_label, metrics_label = (
                    "Descrição",
                    "Destaques",
                    "Métricas",
                )

            top_lines_list.append(
                f"{i + 1}. {confidence_tag} {r.title or '(sem título)'} "
                f"({', '.join(s for s in r.sources if s)}) - score: {r.combined_score}\n"
                f"   {desc_label}: {(r.description or '')[:200]}\n"
                f"   {highlights_label}: {', '.join(h for h in r.highlights if h)}\n"
                f"   {metrics_label}: {r.metrics}"
            )
        top_lines = "\n".join(top_lines_list)

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

        schema = {
            "type": "object",
            "properties": {
                "executive_summary": {
                    "type": "string",
                    "description": "Executive summary of 3-5 sentences about the main findings, in English."
                    if is_english
                    else "Resumo executivo de 3-5 frases sobre os achados principais, em PT-BR.",
                },
                "recommendation": {
                    "type": "string",
                    "description": (
                        "Final recommendation structured in English: (1) main recommendation with concrete data, (2) alternative, (3) next steps (max 3)."
                        if is_english
                        else "Recomendação final estruturada em PT-BR: (1) recomendação principal com dado concreto, (2) alternativa, (3) próximos passos (máx. 3)."
                    ),
                },
                "trends": {
                    "type": "string",
                    "description": (
                        "2-3 technological trends in English, each citing at least one concrete project as evidence."
                        if is_english
                        else "2-3 tendências tecnológicas em PT-BR, cada uma citando pelo menos um projeto concreto como evidência."
                    ),
                },
            },
            "required": ["executive_summary", "recommendation", "trends"],
        }

        if is_english:
            prompt = (
                "You are a senior technical analyst and technology consultant. Write in English.\n"
                "Based on the research data below, generate the THREE narrative sections of a technical report.\n\n"
                "General rules: use concrete data (stars, dates, languages) when available. "
                "Admit limitations when confidence is low. Do not invent information. "
                f"Prioritize sources marked with {high_tag} and handle with caution those marked {low_tag}.\n\n"
                f"Query: {query}\n"
                f"Domain: {metadata.domain}\n"
                f"Sources searched: {', '.join(s for s in metadata.sources if s)}\n"
                f"Results found: {metadata.total_results}\n"
                f"Iterations: {metadata.iterations}\n"
                f"{confidence_note}\n"
                f"{warnings_note}\n\n"
                f"Projects found (ordered by relevance):\n{top_lines}\n\n"
                "Generate the following three fields, each in English:\n"
                "1. executive_summary — 3 to 5 sentences about the main findings.\n"
                "2. recommendation — mandatory structure: (a) Main recommendation, citing a project "
                "and concrete data justifying it; (b) Alternative and when to choose it; "
                "(c) Next steps (maximum 3 specific and actionable actions). Give clear preference "
                f"to {high_tag} projects and avoid recommending {low_tag} items as primary option.\n"
                "3. trends — 2 to 3 technological trends, each citing at least one concrete project "
                "as evidence. Do not extrapolate beyond the data provided."
            )
        else:
            prompt = (
                "Você é um analista técnico sênior e consultor de tecnologia. Escreva em Português do Brasil.\n"
                "Com base nos dados de pesquisa abaixo, gere as TRÊS seções narrativas de um relatório técnico.\n\n"
                "Regras gerais: use dados concretos (stars, datas, linguagens) quando disponíveis. "
                "Admita limitações quando a confiança for baixa. Não invente informações. "
                f"Priorize fontes marcadas com {high_tag} e trate com cautela as marcadas com {low_tag}.\n\n"
                f"Query: {query}\n"
                f"Domínio: {metadata.domain}\n"
                f"Fontes pesquisadas: {', '.join(s for s in metadata.sources if s)}\n"
                f"Resultados encontrados: {metadata.total_results}\n"
                f"Iterações: {metadata.iterations}\n"
                f"{confidence_note}\n"
                f"{warnings_note}\n\n"
                f"Projetos encontrados (ordenados por relevância):\n{top_lines}\n\n"
                "Gere os três campos a seguir, cada um em Português do Brasil:\n"
                "1. executive_summary — 3 a 5 frases sobre os achados principais.\n"
                "2. recommendation — estrutura obrigatória: (a) Recomendação principal, citando um "
                "projeto e um dado concreto que a justifique; (b) Alternativa e quando escolhê-la; "
                "(c) Próximos passos (máximo 3 ações específicas e acionáveis). Dê preferência clara "
                f"a projetos {high_tag} e evite recomendar itens {low_tag} como opção primária.\n"
                "3. trends — 2 a 3 tendências tecnológicas, cada uma citando pelo menos um projeto "
                "concreto como evidência. Não extrapole além dos dados fornecidos."
            )

        data = await self.llm.generate_structured(prompt, schema, temperature=0.35)
        sections = {
            "executive_summary": str(data.get("executive_summary") or "").strip(),
            "recommendation": str(data.get("recommendation") or "").strip(),
            "trends": str(data.get("trends") or "").strip(),
        }
        if not all(sections.values()):
            raise ValueError(
                "Chamada consolidada retornou uma ou mais secoes vazias/ausentes."
            )
        return sections

    async def _generate_executive_summary(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> str:
        """Gera o resumo executivo da pesquisa usando o LLM."""
        is_english = self._is_query_english(query)
        tags = self._get_confidence_tags(is_english)
        high_tag = tags["verified"]
        low_tag = tags["default"]

        top_lines_list = []
        for i, r in enumerate(results[:5]):
            quality = getattr(r, "evidence_quality", "unknown")
            confidence_tag = (
                tags["verified"]
                if quality == "verified"
                else tags["cited"]
                if quality == "cited"
                else tags["default"]
            )
            top_lines_list.append(
                f"{i + 1}. {confidence_tag} {r.title or '(sem título)'} ({', '.join(s for s in r.sources if s)}) - score: {r.combined_score}\n   {(r.description or '')[:200]}..."
            )
        top_lines = "\n".join(top_lines_list)

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

        if is_english:
            prompt = (
                "You are a senior technical analyst. Write in English.\n"
                "Generate an executive summary of 3-5 sentences about the main findings.\n\n"
                "Rules: use concrete data (stars, dates, languages) when available.\n"
                "Admit limitations when confidence is low. Do not invent information.\n"
                f"Prioritize sources marked with {high_tag} and handle with caution sources marked {low_tag}.\n\n"
                f"Query: {query}\n"
                f"Domain: {metadata.domain}\n"
                f"Sources searched: {', '.join(s for s in metadata.sources if s)}\n"
                f"Results found: {metadata.total_results}\n"
                f"Iterations: {metadata.iterations}\n"
                f"{confidence_note}\n"
                f"{warnings_note}\n\n"
                f"Top 5 projects found:\n{top_lines}\n\n"
                "Executive summary:"
            )
        else:
            prompt = (
                "Você é um analista técnico sênior. Escreva em Português do Brasil.\n"
                "Gere um resumo executivo de 3-5 frases sobre os achados principais.\n\n"
                "Regras: use dados concretos (stars, datas, linguagens) quando disponíveis.\n"
                "Admita limitações quando a confiança for baixa. Não invente informações.\n"
                f"Priorize fontes marcadas com {high_tag} e descarte ou mencione com cautela fontes marcadas com {low_tag}.\n\n"
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
            logger.warning(f"LLM executive summary falhou: {e}")
            if is_english:
                return (
                    f"Research on '{query}' found {len(results)} relevant projects "
                    f"in {', '.join(s for s in metadata.sources if s)}."
                )
            return (
                f"Pesquisa sobre '{query}' encontrou {len(results)} projetos relevantes "
                f"em {', '.join(s for s in metadata.sources if s)}."
            )

    async def _generate_recommendation(
        self,
        query: str,
        results: list[SynthesizedResult],
    ) -> str:
        """Gera uma recomendacao estrategica baseada nos resultados da pesquisa."""
        is_english = self._is_query_english(query)
        tags = self._get_confidence_tags(is_english)
        high_tag = tags["verified"]
        low_tag = tags["default"]

        if not results:
            return (
                "No projects found for recommendation."
                if is_english
                else "Nenhum projeto encontrado para recomendação."
            )

        top_lines_list = []
        for i, r in enumerate(results[:5]):
            quality = getattr(r, "evidence_quality", "unknown")
            confidence_tag = (
                tags["verified"]
                if quality == "verified"
                else tags["cited"]
                if quality == "cited"
                else tags["default"]
            )

            if is_english:
                strong_label, metrics_label = "Strong points", "Metrics"
            else:
                strong_label, metrics_label = "Pontos fortes", "Métricas"

            top_lines_list.append(
                f"{i + 1}. {confidence_tag} {r.title or '(sem título)'}\n   {strong_label}: {', '.join(h for h in r.highlights if h)}\n   {metrics_label}: {r.metrics}"
            )
        top_lines = "\n".join(top_lines_list)

        if is_english:
            prompt = (
                "You are a technical consultant. Write in English.\n"
                "Based on the projects found, give a clear and traceable final recommendation.\n\n"
                "Mandatory structure:\n"
                "1. **Main recommendation** — which project and WHY (cite concrete data)\n"
                "2. **Alternative** — second best and when to choose it\n"
                "3. **Next steps** — maximum 3 specific and actionable actions\n\n"
                "Rules: base every statement on the data below. Do not extrapolate beyond the data.\n"
                f"Give clear preference to {high_tag} projects. Avoid recommending {low_tag} as primary option.\n\n"
                f"User query: {query}\n\n"
                f"Projects (ordered by relevance):\n{top_lines}\n\n"
                "Final recommendation:"
            )
        else:
            prompt = (
                "Você é um consultor técnico. Escreva em Português do Brasil.\n"
                "Baseado nos projetos encontrados, dê uma recomendação final clara e rastreável.\n\n"
                "Estrutura obrigatória:\n"
                "1. **Recomendação principal** — qual projeto e POR QUÊ (cite um dado concreto)\n"
                "2. **Alternativa** — segundo melhor e quando escolhê-la\n"
                "3. **Próximos passos** — máximo 3 ações específicas e acionáveis\n\n"
                "Regras: baseie cada afirmação nos dados abaixo. Não extrapole além dos dados.\n"
                f"Dê preferência clara aos projetos marcados com {high_tag}. Evite recomendar itens {low_tag} como opção primária.\n\n"
                f"Query do usuário: {query}\n\n"
                f"Projetos (ordenados por relevância):\n{top_lines}\n\n"
                "Recomendação final:"
            )

        try:
            return await self.llm.generate(prompt, temperature=0.3, max_tokens=800)
        except Exception as e:
            logger.warning(f"LLM recommendation falhou: {e}")
            top = results[0]
            if is_english:
                return f"We recommend **{top.title}** as the primary option. {top.description[:200]}..."
            return f"Recomendamos **{top.title}** como principal opção. {top.description[:200]}..."

    async def _generate_trends(
        self, results: list[SynthesizedResult], query: str | None = None
    ) -> str:
        """Identifica tendencias tecnologicas a partir dos resultados de pesquisa."""
        is_english = query is not None and self._is_query_english(query)
        if len(results) < 3:
            return (
                "Few data points for trends analysis."
                if is_english
                else "Poucos dados para análise de tendências."
            )

        project_lines = "\n".join(
            f"- {r.title or '(sem título)'}: {(r.description or '')[:150]}..."
            for r in results[:8]
        )

        if is_english:
            prompt = (
                "Analyze the projects found and identify 2-3 technological trends in English.\n\n"
                "Rules: each trend MUST cite at least one concrete project as evidence.\n"
                "Do not extrapolate beyond the data. If the data is insufficient, state so.\n\n"
                f"Projects:\n{project_lines}\n\n"
                "Observed trends (in English):"
            )
        else:
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
            logger.warning(f"LLM trends falhou: {e}")
            return (
                "Trends analysis not available."
                if is_english
                else "Análise de tendências não disponível."
            )

    def _assemble_report(
        self,
        query: str,
        metadata: ResearchMetadata,
        results: list[SynthesizedResult],
        executive_summary: str | None,
        recommendation: str | None,
        trends: str | None,
        timeline_section: str = "",
        sentiment_section: str = "",
        comparison_section: str = "",
        perspectives_section: str = "",
    ) -> str:
        """Monta o relatorio final unindo todas as secoes geradas."""
        is_english = self._is_query_english(query)

        # Traduz seções dos analisadores se necessário
        timeline_section = self._translate_analyzer_sections(
            timeline_section, is_english
        )
        sentiment_section = self._translate_analyzer_sections(
            sentiment_section, is_english
        )

        lines = []
        lines.extend(
            self._build_summary(
                query,
                metadata,
                executive_summary,
                comparison_section,
                timeline_section,
                results,
                is_english=is_english,
            )
        )
        lines.extend(self._build_sources(results, is_english=is_english))
        lines.extend(
            self._build_analysis(
                results,
                trends,
                recommendation,
                sentiment_section,
                metadata,
                comparison_section=comparison_section,
                is_english=is_english,
            )
        )
        # FASE 5 — Perspectivas por tom (GDELT): espectro de cobertura favorável
        # vs. crítica quando há contraste de tone entre as fontes.
        if perspectives_section:
            lines.extend(self._translate_perspectives(perspectives_section, is_english))
        # Fase 6.5: seção de referências formatada por DomainPersona
        # (APA / IEEE / Bluebook conforme o domínio da pesquisa).
        lines.extend(self._build_references(results, metadata, is_english=is_english))
        cleaned_lines = [str(line) for line in lines if line is not None]
        return "\n".join(cleaned_lines)

    def _build_perspectives_section(
        self, query: str, results: list[SynthesizedResult]
    ) -> str:
        """Monta a seção de múltiplas perspectivas por tom (GDELT).

        Agrupa os resultados sintetizados que trazem ``metrics.tone`` (tom de
        sentimento do GDELT, escala aproximada -10 a +10) e, quando há
        contraste relevante entre as coberturas (tons favoráveis vs. críticos),
        renderiza uma tabela/lista em Markdown mostrando o espectro de como
        diferentes fontes cobriram o mesmo evento.

        Se nenhum resultado tiver dado de tom, ou se não houver contraste
        suficiente (menos de 2 fontes com tom ou amplitude < 2.0), retorna
        string vazia — a seção é silenciosamente omitida (não quebra o relatório).

        Args:
            query: Query original do usuário (usada para o cabeçalho PT-BR).
            results: Resultados sintetizados (clusters) da pesquisa.

        Returns:
            str: Bloco Markdown da seção de perspectivas, ou "" se não aplicável.
        """
        # Coleta (título, tom, fonte) de resultados com tom numérico válido.
        tone_entries: list[tuple[str, float, str]] = []
        for r in results:
            tone = (r.metrics or {}).get("tone")
            if isinstance(tone, (int, float)):
                label = (r.title or "(sem título)").strip()
                source = r.sources[0] if r.sources else "desconhecida"
                tone_entries.append((label, float(tone), source))

        # Sem dados de tom ou sem contraste mínimo → omitir seção.
        if len(tone_entries) < 2:
            return ""
        tones = [t for _, t, _ in tone_entries]
        if max(tones) - min(tones) < 2.0:
            return ""

        # Ordena do tom mais favorável (maior) para o mais crítico (menor).
        tone_entries.sort(key=lambda x: x[1], reverse=True)

        is_english = self._is_query_english(query)
        header = "## 🌈 Espectro de Perspectivas (Tom da Cobertura)"
        if is_english:
            header = "## 🌈 Perspective Spectrum (Coverage Tone)"

        lines = [header, ""]
        if is_english:
            lines.append(
                "How different sources covered the same event, by GDELT tone "
                "(positive = favorable, negative = critical):"
            )
        else:
            lines.append(
                "Como diferentes fontes cobriram o mesmo evento, pelo tom do GDELT "
                "(positivo = favorável, negativo = crítico):"
            )
        lines.append("")

        for label, tone, source in tone_entries:
            if tone > 2.0:
                badge = "🟢 Favorável" if not is_english else "🟢 Favorable"
            elif tone < -2.0:
                badge = "🔴 Crítico" if not is_english else "🔴 Critical"
            else:
                badge = "⚪ Neutro" if not is_english else "⚪ Neutral"
            lines.append(f"- {badge} **{label}** ({source}) — tom: `{tone:+.2f}`")

        lines.append("")
        return "\n".join(lines)

    def _translate_perspectives(self, section: str, is_english: bool) -> list[str]:
        """Traduz o cabeçalho fixo da seção de perspectivas se necessário.

        A seção já é gerada no idioma correto por ``_build_perspectives_section``,
        mas esta função garante consistência caso a seção venha de cache ou de
        outra origem, realinhando o cabeçalho ao idioma da query.

        Args:
            section: Bloco Markdown da seção de perspectivas.
            is_english: True se a query está em inglês.

        Returns:
            list[str]: Linhas da seção (já traduzidas se aplicável).
        """
        if is_english:
            section = section.replace(
                "## 🌈 Espectro de Perspectivas (Tom da Cobertura)",
                "## 🌈 Perspective Spectrum (Coverage Tone)",
            )
            section = section.replace(
                "Como diferentes fontes cobriram o mesmo evento, pelo tom do GDELT "
                "(positivo = favorável, negativo = crítico):",
                "How different sources covered the same event, by GDELT tone "
                "(positive = favorable, negative = critical):",
            )
        return section.split("\n")

    def _build_summary(
        self,
        query: str,
        metadata: ResearchMetadata,
        executive_summary: str | None,
        comparison_section: str,
        timeline_section: str,
        results: list[SynthesizedResult],
        is_english: bool = False,
    ) -> list[str]:
        """Constroi o bloco de cabecalho e resumo executivo do relatorio."""
        timestamp = metadata.timestamp.strftime("%Y-%m-%d %H:%M")
        exec_summary_clean = (executive_summary or "").strip()
        if not exec_summary_clean:
            if is_english:
                exec_summary_clean = (
                    f"Research completed successfully for '{query}'. Found {len(results)} "
                    f"relevant projects in searched sources ({', '.join(s for s in metadata.sources if s)}). "
                    f"See the list of detailed tools below for more information."
                )
            else:
                exec_summary_clean = (
                    f"Pesquisa realizada com sucesso sobre '{query}'. Foram encontrados {len(results)} "
                    f"projetos relevantes nas fontes pesquisadas ({', '.join(s for s in metadata.sources if s)}). "
                    f"Consulte a lista de ferramentas detalhadas abaixo para obter mais informações."
                )

        if is_english:
            lines = [
                f"# Report: {query}",
                "",
                f"> Generated on: {timestamp}  ",
                f"> Searched sources: {', '.join(s for s in metadata.sources if s)}  ",
                f"> Results found: {metadata.total_results}  ",
                f"> Search iterations: {metadata.iterations}  ",
                f"> Total time: {round(metadata.duration_seconds, 1)}s",
                "",
                "---",
                "",
                "## 1. Executive Summary",
                "",
                exec_summary_clean,
                "",
                "---",
            ]
        else:
            lines = [
                f"# Relatório: {query}",
                "",
                f"> Gerado em: {timestamp}  ",
                f"> Fontes pesquisadas: {', '.join(s for s in metadata.sources if s)}  ",
                f"> Resultados encontrados: {metadata.total_results}  ",
                f"> Iterações de pesquisa: {metadata.iterations}  ",
                f"> Tempo total: {round(metadata.duration_seconds, 1)}s",
                "",
                "---",
                "",
                "## 1. Resumo Executivo",
                "",
                exec_summary_clean,
                "",
                "---",
            ]
        return lines

    def _build_sources(
        self, results: list[SynthesizedResult], is_english: bool = False
    ) -> list[str]:
        """Constroi o bloco detalhado de projetos e ferramentas encontradas."""
        if is_english:
            lines = [
                "## 2. Discovered Projects / Tools",
                "",
            ]
            stars_label, forks_label, comments_label, upvotes_label, updated_label = (
                "Stars",
                "Forks",
                "Comments",
                "Upvotes",
                "Updated",
            )
        else:
            lines = [
                "## 2. Projetos / Ferramentas Encontradas",
                "",
            ]
            stars_label, forks_label, comments_label, upvotes_label, updated_label = (
                "Stars",
                "Forks",
                "Comentários",
                "Upvotes",
                "Atualizado",
            )

        for i, r in enumerate(results[:15]):
            metric_parts = []
            if "stars" in r.metrics:
                metric_parts.append(f"{stars_label}: {r.metrics['stars']}")
            if "forks" in r.metrics:
                metric_parts.append(f"{forks_label}: {r.metrics['forks']}")
            if "comments" in r.metrics:
                metric_parts.append(f"{comments_label}: {r.metrics['comments']}")
            elif "upvotes" in r.metrics:
                metric_parts.append(f"{upvotes_label}: {r.metrics['upvotes']}")
            if "updated_at" in r.metrics:
                metric_parts.append(
                    f"{updated_label}: {str(r.metrics['updated_at'])[:10]}"
                )
            metrics_str = " | ".join(metric_parts)

            highlights_str = "\n".join(f"- {h}" for h in r.highlights if h) or (
                "- No specific highlights"
                if is_english
                else "- Nenhum destaque específico"
            )
            desc_text = (r.description or "")[:300] + (
                "..." if len(r.description or "") > 300 else ""
            )

            verdict = getattr(r, "verdict", "") or ""
            tldr = getattr(r, "tldr", "") or ""
            next_step = getattr(r, "next_step", "") or ""
            read_min = getattr(r, "read_min", 0) or 0

            if is_english:
                verdict_icons = {
                    "Foca": "🔴 Focus",
                    "Considera": "🟡 Consider",
                    "Acompanha": "🟢 Watch",
                    "Ignora": "⚪ Ignore",
                }
                quality_badges = {
                    "verified": "🌟 Verified (High Confidence)",
                    "cited": "📖 Cited (Medium Confidence)",
                    "inferred": "🔍 Inferred (Low Confidence)",
                    "unknown": "❓ Unknown",
                }
            else:
                verdict_icons = {
                    "Foca": "🔴 Foca",
                    "Considera": "🟡 Considera",
                    "Acompanha": "🟢 Acompanha",
                    "Ignora": "⚪ Ignora",
                }
                quality_badges = {
                    "verified": "🌟 Verificado (Alta Confiança)",
                    "cited": "📖 Citado (Confiança Média)",
                    "inferred": "🔍 Inferido (Confiança Baixa)",
                    "unknown": "❓ Desconhecido",
                }

            verdict_display = verdict_icons.get(verdict, verdict)
            quality_display = quality_badges.get(
                evidence_quality := getattr(r, "evidence_quality", "unknown"),
                evidence_quality,
            )

            is_single_source = len(r.sources) <= 1
            if is_english:
                source_warning = " | ⚠️ **Single Source**" if is_single_source else ""
            else:
                source_warning = (
                    " | ⚠️ **Fonte Única (Single Source)**" if is_single_source else ""
                )

            flags = getattr(r, "hallucination_flags", []) or []
            flags_display = ""
            if flags:
                if is_english:
                    flag_labels = {
                        "stale_content": "Stale Content",
                        "opinion_content": "Subjective/Opinion",
                        "circular_reference": "Circular Reference (Echo Chamber)",
                        "dead_links_detected": "Broken Links Detected",
                        "content_too_short": "Content Too Short",
                        "content_brief": "Content Brief",
                        "untrusted_domain": "Untrusted Domain",
                        "clickbait_title": "Clickbait Title",
                        "absolute_claim_detected": "Absolute Claim",
                    }
                    flags_display = " | 🚫 **Alerts:** " + ", ".join(
                        flag_labels.get(f, f) for f in flags
                    )
                else:
                    flag_labels = {
                        "stale_content": "Conteúdo Desatualizado",
                        "opinion_content": "Subjetivo/Opinião",
                        "circular_reference": "Circularidade de Referências (Echo Chamber)",
                        "dead_links_detected": "Links Quebrados Detectados",
                        "content_too_short": "Conteúdo Muito Curto",
                        "content_brief": "Conteúdo Sucinto",
                        "untrusted_domain": "Domínio Não Confiável",
                        "clickbait_title": "Título Clickbait",
                        "absolute_claim_detected": "Afirmação Absoluta",
                    }
                    flags_display = " | 🚫 **Alertas:** " + ", ".join(
                        flag_labels.get(f, f) for f in flags
                    )

            entry_lines = [
                f"### 2.{i + 1} {r.title or '(sem título)'}",
            ]

            if verdict_display:
                if is_english:
                    entry_lines.append(
                        f"> **Verdict:** {verdict_display}  |  ⏱️ ~{read_min} min  |  **Quality:** {quality_display}{source_warning}{flags_display}"
                    )
                else:
                    entry_lines.append(
                        f"> **Veredito:** {verdict_display}  |  ⏱️ ~{read_min} min  |  **Qualidade:** {quality_display}{source_warning}{flags_display}"
                    )

            if tldr:
                entry_lines.append(f"> {tldr}")

            if is_english:
                entry_lines += [
                    f"- **Description:** {desc_text}",
                    f"- **URLs:** {', '.join(u for u in r.urls[:3] if u)}",
                    f"- **Sources:** {', '.join(s for s in r.sources if s)}",
                    f"- **Score:** {r.combined_score}/100",
                    f"- **Metrics:** {metrics_str}",
                    f"- **Highlights:**\n{highlights_str}",
                ]
                if next_step:
                    entry_lines.append(f"- **Next Action:** {next_step}")
            else:
                entry_lines += [
                    f"- **Descrição:** {desc_text}",
                    f"- **URLs:** {', '.join(u for u in r.urls[:3] if u)}",
                    f"- **Fontes:** {', '.join(s for s in r.sources if s)}",
                    f"- **Score:** {r.combined_score}/100",
                    f"- **Métricas:** {metrics_str}",
                    f"- **Destaques:**\n{highlights_str}",
                ]
                if next_step:
                    entry_lines.append(f"- **Próxima Ação:** {next_step}")

            entry_lines.append("")
            lines += entry_lines
        return lines

    # ── Fase 6.5: Referências formatadas por DomainPersona ────────────

    def _build_references(
        self,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
        is_english: bool = False,
    ) -> list[str]:
        """Constroi a seção de referências formatada academicamente.

        Religa o ``DomainPersona`` (``src/domain_personas.py``), que
        formata cada citação segundo a norma do domínio da pesquisa:
          - Domínios técnicos (dev_tools, ai_ml, infrastructure, open_source)
            → IEEE.
          - Demais (saas_b2b, automation, general) → APA.

        (Bluebook é exposto pela API ``format_bluebook`` para uso legal
        explícito; o relatório padrão usa APA/IEEE conforme o domínio.)

        Cada ``SynthesizedResult`` é adaptado para ``SearchResult``
        (o contrato esperado por ``DomainPersona``), preservando título,
        URL, fonte e metadados de coleta.
        """
        from src.domain_personas import DomainPersona

        domain_str = getattr(metadata, "domain", "") or "general"
        domain = self._resolve_domain(domain_str)

        try:
            persona = DomainPersona(domain)
        except Exception as e:  # pragma: no cover - defensivo
            logger.warning("ReportGenerator: falha ao criar DomainPersona: %s", e)
            return []

        refs: list[str] = []
        for i, r in enumerate(results[:15]):
            sr = self._to_search_result(r)
            if sr is None:
                continue
            try:
                citation = persona.format_citation(sr, index=i + 1)
            except Exception as e:  # pragma: no cover - defensivo
                logger.warning("ReportGenerator: falha ao formatar citação: %s", e)
                continue
            if citation and citation.strip():
                refs.append(citation.strip())

        if not refs:
            return []

        if is_english:
            header = "## 9. References"
            note = (
                "_Formatted per "
                + self._style_name(persona, domain)
                + " citation style for the '"
                + domain_str
                + "' domain._"
            )
        else:
            header = "## 9. Referências"
            note = (
                "_Formatado segundo a norma "
                + self._style_name(persona, domain)
                + " para o domínio '"
                + domain_str
                + "'._"
            )
        return ["---", "", header, "", note, ""] + refs

    @staticmethod
    def _resolve_domain(domain_str: str) -> "Domain":
        """Mapeia a string de domínio (metadata) para o enum ``Domain``."""
        try:
            return Domain(domain_str)
        except Exception:
            return Domain.GENERAL

    @staticmethod
    def _style_name(persona: Any, domain: "Domain") -> str:
        """Retorna o nome legível da norma aplicada (APA/IEEE/Bluebook)."""
        try:
            func_name = getattr(
                persona._formatter, "__func__", persona._formatter
            ).__name__
            if func_name == "format_ieee":
                return "IEEE"
            if func_name == "format_bluebook":
                return "Bluebook"
        except Exception:
            pass
        return "APA"

    @staticmethod
    def _to_search_result(r: SynthesizedResult) -> SearchResult | None:
        """Adapta um ``SynthesizedResult`` para o contrato ``SearchResult``."""
        from datetime import datetime

        if r is None:
            return None
        source = (r.sources[0] if r.sources else "synthesis") or "synthesis"
        url = (r.urls[0] if r.urls else "") or ""
        title = getattr(r, "title", "") or ""
        if not title and not url:
            return None
        fetched_at = getattr(r, "last_seen", None) or getattr(r, "first_seen", None)
        if fetched_at is None:
            fetched_at = datetime.now()
        raw: dict[str, Any] = {}
        # Preserva a data de coleta bruta, se presente, para o DomainPersona.
        if isinstance(fetched_at, datetime):
            try:
                raw["published_date"] = fetched_at.isoformat()
            except Exception:
                pass
        return SearchResult(
            source=source,
            title=title,
            url=url,
            description=getattr(r, "description", "") or "",
            metrics=dict(getattr(r, "metrics", {}) or {}),
            raw=raw,
            fetched_at=fetched_at,
        )

    def _build_analysis(
        self,
        results: list[SynthesizedResult],
        trends: str | None,
        recommendation: str | None,
        sentiment_section: str,
        metadata: ResearchMetadata,
        comparison_section: str = "",
        is_english: bool = False,
    ) -> list[str]:
        """Constroi o bloco de analise com tendencias, recomendacao e sentimento."""
        recommendation_clean = (recommendation or "").strip()
        if not recommendation_clean:
            if is_english:
                recommendation_clean = (
                    "### Automatic Recommendation\n"
                    "Based on available data, we suggest prioritizing projects with higher relevance scores "
                    "and continuous activity in the repository. Check the comparison table for additional details."
                )
            else:
                recommendation_clean = (
                    "### Recomendação Automática\n"
                    "Com base nos dados disponíveis, sugerimos priorizar os projetos com maiores pontuações de relevância "
                    "e atividade contínua no repositório. Verifique a tabela de comparação para detalhes adicionais."
                )

        trends_clean = (trends or "").strip()
        if not trends_clean:
            if is_english:
                trends_clean = (
                    "- **Focus on Simplified Integration**: Growth of out-of-the-box tools and CDNs.\n"
                    "- **Security and Privacy**: Focus on self-hosted solutions and RLS/mTLS policies."
                )
            else:
                trends_clean = (
                    "- **Foco em Integração Simplificada**: Crescimento de ferramentas prontas e CDNs.\n"
                    "- **Segurança e Privacidade**: Foco em soluções self-hosted e políticas de RLS/mTLS."
                )

        lines = ["---", ""]

        # Seção 3: Comparação (Usa o Comparator se disponível, senão tabela genérica)
        if comparison_section:
            # O Comparator já gera um cabeçalho "## ⚖️ Comparação Side-by-Side"
            # Ajustamos para manter a numeração do relatório
            if is_english:
                comparison_section = comparison_section.replace(
                    "## ⚖️ Comparação Side-by-Side", "## 3. ⚖️ Side-by-Side Comparison"
                )
            else:
                comparison_section = comparison_section.replace(
                    "## ⚖️ Comparação Side-by-Side", "## 3. ⚖️ Comparação Side-by-Side"
                )
            lines.append(comparison_section)
            lines.append("")
        else:
            # Fallback: Tabela genérica
            if is_english:
                lines += [
                    "## 3. Side-by-Side Comparison",
                    "",
                    "| Project | Stars | Forks | Updated | License | Score | Verdict |",
                    "|---------|-------|-------|-------------|---------|-------|----------|",
                ]
            else:
                lines += [
                    "## 3. Comparação Lado a Lado",
                    "",
                    "| Projeto | Stars | Forks | Atualização | Licença | Score | Veredito |",
                    "|---------|-------|-------|-------------|---------|-------|----------|",
                ]

            for r in results[:10]:
                stars = r.metrics.get("stars", "-")
                forks = r.metrics.get("forks", "-")
                updated = str(r.metrics.get("updated_at", "-"))[:10]
                license_id = r.metrics.get("license", "-")
                verdict = getattr(r, "verdict", "") or "-"
                if is_english:
                    verdict_translations = {
                        "Foca": "Focus",
                        "Considera": "Consider",
                        "Acompanha": "Watch",
                        "Ignora": "Ignore",
                    }
                    verdict = verdict_translations.get(verdict, verdict)
                lines.append(
                    f"| {(r.title or '')[:30]} | {stars} | {forks} | {updated} | {license_id} | {r.combined_score} | {verdict} |"
                )
            lines.append("")

        if is_english:
            lines += [
                "---",
                "",
                "## 4. Identified Technologies / Stacks",
                "",
            ]
        else:
            lines += [
                "---",
                "",
                "## 4. Tecnologias / Stacks Identificadas",
                "",
            ]

        languages: dict = {}
        for r in results:
            lang = r.metrics.get("language")
            if lang:
                languages.setdefault(lang, []).append(r.title or "(sem título)")
        for lang, projects in sorted(
            languages.items(), key=lambda x: len(x[1]), reverse=True
        )[:10]:
            proj_str = ", ".join(projects[:3]) + ("..." if len(projects) > 3 else "")
            if is_english:
                lines.append(f"- **{lang}** — used by {proj_str}")
            else:
                lines.append(f"- **{lang}** — usado por {proj_str}")

        if is_english:
            lines += [
                "",
                "---",
                "",
                "## 5. Community Discussion",
                "",
            ]
        else:
            lines += [
                "",
                "---",
                "",
                "## 5. Discussão da Comunidade",
                "",
            ]

        reddit_results = [r for r in results if "reddit" in r.sources]
        hn_results = [r for r in results if "hackernews" in r.sources]
        if reddit_results:
            lines.append("### Reddit")
            for r in reddit_results[:3]:
                sub = r.metrics.get("subreddit", "unknown")
                upvotes = r.metrics.get("upvotes", 0)
                lines.append(
                    f"- **r/{sub}**: {(r.title or '')[:80]}... ({upvotes} upvotes)"
                )
            lines.append("")
        if hn_results:
            lines.append("### Hacker News")
            for r in hn_results[:3]:
                points = r.metrics.get("points", 0)
                author = r.metrics.get("author", "unknown")
                lines.append(
                    f"- **{author}**: {(r.title or '')[:80]}... ({points} points)"
                )
            lines.append("")

        if sentiment_section:
            lines += [
                "---",
                "",
                sentiment_section,
                "",
            ]

        if is_english:
            lines += [
                "---",
                "",
                "## 6. Trend Analysis",
                "",
                trends_clean,
                "",
                "---",
                "",
                "## 7. Final Recommendation",
                "",
                recommendation_clean,
                "",
                "---",
                "",
                "## 8. Links and References",
                "",
            ]
        else:
            lines += [
                "---",
                "",
                "## 6. Análise de Tendências",
                "",
                trends_clean,
                "",
                "---",
                "",
                "## 7. Recomendação Final",
                "",
                recommendation_clean,
                "",
                "---",
                "",
                "## 8. Links e Referências",
                "",
            ]

        seen = set()
        all_urls = []
        for r in results[:20]:
            for url in r.urls:
                if url not in seen:
                    seen.add(url)
                    all_urls.append(url)
        for i, url in enumerate(all_urls[:20], 1):
            lines.append(f"{i}. [{url}]({url})")

        all_dead_links = set()
        for r in results:
            dead = r.metrics.get("dead_links", [])
            if dead:
                all_dead_links.update(dead)

        if all_dead_links:
            if is_english:
                lines += [
                    "",
                    "### Invalid or Broken Links Detected",
                    "The following references cited by the original sources failed connection tests (404, timeouts, or inaccessible):",
                    "",
                ]
            else:
                lines += [
                    "",
                    "### Links Inválidos ou Quebrados Detectados",
                    "As seguintes referências citadas pelas fontes originais falharam nos testes de conexão (404, timeouts ou inacessíveis):",
                    "",
                ]
            for url in sorted(all_dead_links):
                lines.append(f"- ❌ {url}")

        if metadata.low_confidence_warnings:
            if is_english:
                lines += [
                    "",
                    "---",
                    "",
                    "## 9. Warnings and Limitations",
                    "",
                ]
                for w in metadata.low_confidence_warnings:
                    lines.append(f"- ⚠️ {w}")
                if metadata.overall_confidence < 0.6:
                    lines.append(
                        "- ⚠️ Overall confidence below 60% — additional research recommended."
                    )
            else:
                lines += [
                    "",
                    "---",
                    "",
                    "## 9. Advertências e Limitações",
                    "",
                ]
                for w in metadata.low_confidence_warnings:
                    lines.append(f"- ⚠️ {w}")
                if metadata.overall_confidence < 0.6:
                    lines.append(
                        "- ⚠️ Confiança geral abaixo de 60% — pesquisa adicional recomendada."
                    )

        if is_english:
            footer_text = f"*Report generated by Smart Research Agent v2.0 | {metadata.timestamp.strftime('%Y-%m-%d %H:%M')}*"
        else:
            footer_text = f"*Relatório gerado por Smart Research Agent v2.0 | {metadata.timestamp.strftime('%Y-%m-%d %H:%M')}*"

        lines += [
            "",
            "---",
            "",
            footer_text,
        ]
        return lines

    def save_report(
        self,
        report: str,
        query: str,
        output_dir: str = "./reports",
        formats: list[ReportFormat] | None = None,
        results: list[SynthesizedResult] | None = None,
    ) -> str:
        """Salva o relatório no disco.

        Args:
            report: Conteúdo Markdown do relatório.
            query: Query original da pesquisa (usada para gerar o slug do arquivo).
            output_dir: Diretório de saída.
            formats: Formatos de exportação adicionais além do Markdown (PDF, DOCX,
                PPTX, BIBTEX, RIS).
            results: Resultados sintetizados da pesquisa. Necessários apenas para os
                formatos de citação (BIBTEX/RIS); opcional para os demais.
        """
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        normalized = (
            unicodedata.normalize("NFKD", query)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        slug = re.sub(r"[^a-z0-9\-]+", "-", normalized.lower())
        slug = re.sub(r"-+", "-", slug).strip("-")[:50]
        if not slug:
            slug = "report"

        base_name = datetime.now().strftime("%Y-%m-%d") + f"-{slug}"
        md_path = os.path.join(output_dir, f"{base_name}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info(f"Relatório salvo em: {md_path}")

        # Dispatcher de formatos adicionais
        extra_formats = set(formats or [])
        if ReportFormat.PDF in extra_formats:
            try:
                from src.exporters.pdf_exporter import PDFExporter

                pdf_result = PDFExporter().export(report, md_path)
                if pdf_result:
                    logger.info(f"PDF exportado: {pdf_result}")
            except Exception as e:
                logger.warning(f"Falha na exportação PDF (não crítico): {e}")

        if ReportFormat.DOCX in extra_formats:
            try:
                from src.exporters.docx_exporter import DOCXExporter

                docx_result = DOCXExporter().export(report, md_path)
                if docx_result:
                    logger.info(f"DOCX exportado: {docx_result}")
            except Exception as e:
                logger.warning(f"Falha na exportação DOCX (não crítico): {e}")

        if ReportFormat.PPTX in extra_formats:
            try:
                from src.exporters.pptx_exporter import PPTXExporter

                pptx_result = PPTXExporter().export(report, md_path)
                if pptx_result:
                    logger.info(f"PPTX exportado: {pptx_result}")
            except Exception as e:
                logger.warning(f"Falha na exportação PPTX (não crítico): {e}")

        if ReportFormat.BIBTEX in extra_formats:
            try:
                from src.exporters.bibtex_exporter import BibTeXExporter

                citations = self._build_citation_dicts(results)
                if citations:
                    bib_path = os.path.join(output_dir, f"{base_name}.bib")
                    BibTeXExporter.export_batch(citations, filename=bib_path)
                    logger.info(f"BibTeX exportado: {bib_path}")
            except Exception as e:
                logger.warning(f"Falha na exportação BibTeX (não crítico): {e}")

        if ReportFormat.RIS in extra_formats:
            try:
                from src.exporters.ris_exporter import RISExporter

                citations = self._build_citation_dicts(results)
                if citations:
                    ris_path = os.path.join(output_dir, f"{base_name}.ris")
                    RISExporter.export_batch(citations, filename=ris_path)
                    logger.info(f"RIS exportado: {ris_path}")
            except Exception as e:
                logger.warning(f"Falha na exportação RIS (não crítico): {e}")

        return md_path

    def _build_citation_dicts(
        self, results: list[SynthesizedResult] | None
    ) -> list[dict]:
        """Converte resultados sintetizados em dicionários de citação para os exporters.

        Os `BibTeXExporter`/`RISExporter` consomem uma lista de dicts com as chaves
        ``title``, ``authors``, ``year``, ``url`` e ``source``. Aqui extraímos esses
        campos de cada `SynthesizedResult`, usando a primeira URL disponível e a
        primeira fonte como metadados de citação. Se ``results`` for None ou vazio,
        retorna lista vazia (exportação de citação é pulada silenciosamente).

        Args:
            results: Lista de resultados sintetizados do relatório.

        Returns:
            list[dict]: Dicionários de citação prontos para ``export_batch``.
        """
        if not results:
            return []
        citations: list[dict] = []
        for r in results:
            if not getattr(r, "title", None):
                continue
            urls = getattr(r, "urls", []) or []
            sources = getattr(r, "sources", []) or []
            metrics = getattr(r, "metrics", {}) or {}
            citations.append(
                {
                    "title": r.title,
                    "authors": metrics.get("authors", []),
                    "year": metrics.get("year", datetime.now().year),
                    "url": urls[0] if urls else "",
                    "source": sources[0] if sources else "web",
                }
            )
        return citations
