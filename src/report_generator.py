"""Módulo de geração de relatórios de pesquisa em Markdown.

Orquestra a montagem de relatórios estruturados a partir de resultados sintetizados,
combinando sumário executivo gerado por LLM, análise de fontes, tendências,
sentimento, comparações e timeline cronológica.
"""

import asyncio
import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path

from src.cache import Cache
from src.clients.llm_client import LLMClient
from src.comparator import Comparator
from src.sentiment_analyzer import SentimentAnalyzer
from src.temporal_analyzer import TemporalAnalyzer
from src.types import ReportFormat, ResearchMetadata, SynthesizedResult

logger = logging.getLogger(__name__)

# TTL do cache de secoes LLM (resumo/recomendacao/tendencias) do relatorio.
# Cobre reexecucoes/reexportacoes (md+pdf+docx) do mesmo conjunto de resultados
# dentro de uma mesma sessao de pesquisa, sem gastar chamadas de LLM extras.
_SECTIONS_CACHE_TTL_SECONDS = 1800  # 30 minutos

# Schema JSON para a chamada consolidada das 3 secoes narrativas do relatorio.
_SECTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {
            "type": "string",
            "description": "Resumo executivo de 3-5 frases sobre os achados principais, em PT-BR.",
        },
        "recommendation": {
            "type": "string",
            "description": (
                "Recomendacao final estruturada em PT-BR: (1) recomendacao principal "
                "com dado concreto, (2) alternativa, (3) proximos passos (max. 3)."
            ),
        },
        "trends": {
            "type": "string",
            "description": (
                "2-3 tendencias tecnologicas em PT-BR, cada uma citando pelo menos "
                "um projeto concreto como evidencia."
            ),
        },
    },
    "required": ["executive_summary", "recommendation", "trends"],
}


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
        # Aceita um Cache compartilhado (ex: injetado pelo Orchestrator) ou
        # cria um proprio com os defaults do projeto (./.cache, sem Redis).
        self.cache = cache or Cache()

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
        if words & en_words:
            if not (words & pt_words):
                return True
        if "vs" in words or "versus" in words:
            return True
        return False

    async def generate(
        self,
        query: str,
        results: list[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> str:
        """Gera o relatório completo de pesquisa como string Markdown.

        Args:
            query: Query original do usuário.
            results: Lista de `SynthesizedResult` ordenada por score.
            metadata: Metadados da sessão de pesquisa (duracao, fontes, etc).

        Returns:
            str: Relatório completo em formato Markdown.
        """
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
        comparison_section = self.comparator.generate_comparison_section(query, results)

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
        )
        return await self._validate_and_enrich_sections(report_raw, query, results)

    async def _validate_and_enrich_sections(
        self, report_md: str, query: str, results: list[SynthesizedResult]
    ) -> str:
        """Valida se as seções do relatório gerado são muito curtas ou vazias e enriquece se necessário (BUG-13)."""
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
                    if "Technologies / Stacks" in header or "Tecnologias" in header:
                        prompt += (
                            "- Identify likely languages (e.g. Rust, Python, TypeScript) and why they are used.\n"
                            "- Elaborate on the transport architecture (HTTP, WebSockets, stdio) commonly employed.\n"
                            "- Detail dependencies and ecosystems involved (e.g. tokio, async-trait in Rust, fastmcp in Python)."
                        )
                    elif (
                        "Community Discussion" in header
                        or "Discussao" in header
                        or "Discussão" in header
                    ):
                        prompt += (
                            "- Synthesize the overall reception of this type of technology by the developer community.\n"
                            "- Discuss main bottlenecks discussed (learning curve, data security in LLMs).\n"
                            "- Cite observed interest through stars and general adoption discussions of the MCP protocol."
                        )
                    elif "Sentiment" in header or "Sentimento" in header:
                        prompt += (
                            "- Describe the general tone of mentions (optimistic, pragmatic, skeptical).\n"
                            "- Point out reasons for enthusiasm (flexible agent automation) and sources of skepticism (security, latency)."
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

                    if "Tecnologias / Stacks" in header or "Tecnologias" in header:
                        prompt += (
                            "- Identifique as linguagens prováveis (ex: Rust, Python, TypeScript) e por que são usadas.\n"
                            "- Discorra sobre a arquitetura de transporte (HTTP, WebSockets, stdio) comumente empregada.\n"
                            "- Detalhe as dependências e ecossistemas envolvidos (ex: tokio, async-trait no Rust, fastmcp no Python)."
                        )
                    elif (
                        "Discussao da Comunidade" in header
                        or "Discussão" in header
                        or "Discussao" in header
                    ):
                        prompt += (
                            "- Sintetize a recepção geral desse tipo de tecnologia pela comunidade de desenvolvedores.\n"
                            "- Fale sobre os principais gargalos discutidos (curva de aprendizado, segurança de dados em LLMs).\n"
                            "- Cite o interesse observado através das estrelas e discussões gerais de adoção do protocolo MCP."
                        )
                    elif "Sentimento" in header or "Sentimento" in header:
                        prompt += (
                            "- Descreva o tom geral das menções (otimista, pragmático, cético).\n"
                            "- Aponte os motivos do entusiasmo (automação flexível de agentes) e as fontes de ceticismo (segurança, latência)."
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
        """Gera uma chave de cache determinística para as 3 secoes narrativas.

        A chave depende da query, do dominio/total de resultados e de uma
        "impressao digital" dos top-8 resultados (titulo, score, fontes e
        qualidade de evidencia). Isso garante cache-hit em reexecucoes ou
        reexportacoes (md/pdf/docx) do mesmo conjunto de resultados sem
        depender de identidade de objeto.
        """
        fingerprint_parts = [
            query.strip().lower(),
            str(metadata.domain),
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
        """Obtem as 3 secoes narrativas do relatorio (resumo, recomendacao, tendencias).

        Estrategia (do mais para o menos eficiente):
          1. Cache — reaproveita secoes ja geradas para o mesmo conjunto de resultados.
          2. Chamada consolidada — 1 unica chamada LLM com schema JSON estruturado.
          3. Fallback paralelo — se a consolidada falhar (JSON invalido, erro de
             API, dados insuficientes), executa as 3 chamadas originais em
             paralelo via `asyncio.gather()`, cada uma com seu proprio fallback
             textual individual (comportamento identico ao anterior).

        Args:
            query: Query original do usuario.
            results: Resultados sintetizados para contexto do LLM.
            metadata: Metadados da sessao de pesquisa.

        Returns:
            dict com as chaves "executive_summary", "recommendation" e "trends".
        """

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

        # Casos degenerados (sem resultados ou poucos demais para tendencias
        # confiaveis) usam diretamente o caminho paralelo: os metodos originais
        # ja tem seus proprios guard-clauses para esses casos (ex: "Poucos dados
        # para analise de tendencias"), e o volume de dados nao justifica cache.
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
        """Gera resumo executivo, recomendacao e tendencias em 1 unica chamada LLM.

        Consolida os 3 prompts originais em um so, solicitando resposta em
        JSON estruturado via `LLMClient.generate_structured`. Substitui 3
        round-trips sequenciais (ou paralelos) por apenas 1.

        Raises:
            Exception: se a chamada LLM falhar ou o JSON retornado nao tiver
                todas as secoes preenchidas — o chamador deve tratar isso e
                cair no fallback paralelo com os prompts individuais.
        """
        is_english = self._is_query_english(query)
        top_lines_list = []
        for i, r in enumerate(results[:8]):
            quality = getattr(r, "evidence_quality", "unknown")
            confidence_tag = (
                "[ALTA CONFIANÇA]"
                if quality == "verified"
                else "[MÉDIA]"
                if quality == "cited"
                else "[BAIXA — VERIFICAR]"
            )
            top_lines_list.append(
                f"{i+1}. {confidence_tag} {r.title or '(sem título)'} "
                f"({', '.join(s for s in r.sources if s)}) - score: {r.combined_score}\n"
                f"   Descricao: {(r.description or '')[:200]}\n"
                f"   Destaques: {', '.join(h for h in r.highlights if h)}\n"
                f"   Metricas: {r.metrics}"
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
                        else "Recomendacao final estruturada em PT-BR: (1) recomendacao principal com dado concreto, (2) alternativa, (3) proximos passos (max. 3)."
                    ),
                },
                "trends": {
                    "type": "string",
                    "description": (
                        "2-3 technological trends in English, each citing at least one concrete project as evidence."
                        if is_english
                        else "2-3 tendencias tecnologicas em PT-BR, cada uma citando pelo menos um projeto concreto como evidencia."
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
                "Prioritize sources marked with [ALTA CONFIANÇA] and handle with caution those marked [BAIXA — VERIFICAR].\n\n"
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
                "to [ALTA CONFIANÇA] projects and avoid recommending [BAIXA — VERIFICAR] items as primary option.\n"
                "3. trends — 2 to 3 technological trends, each citing at least one concrete project "
                "as evidence. Do not extrapolate beyond the data provided."
            )
        else:
            prompt = (
                "Você é um analista técnico sênior e consultor de tecnologia. Escreva em Português do Brasil.\n"
                "Com base nos dados de pesquisa abaixo, gere as TRÊS seções narrativas de um relatório técnico.\n\n"
                "Regras gerais: use dados concretos (stars, datas, linguagens) quando disponíveis. "
                "Admita limitações quando a confiança for baixa. Não invente informações. "
                "Priorize fontes marcadas com [ALTA CONFIANÇA] e trate com cautela as marcadas com [BAIXA — VERIFICAR].\n\n"
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
                "a projetos [ALTA CONFIANÇA] e evite recomendar itens [BAIXA — VERIFICAR] como opção primária.\n"
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
        top_lines_list = []
        for i, r in enumerate(results[:5]):
            quality = getattr(r, "evidence_quality", "unknown")
            confidence_tag = (
                "[ALTA CONFIANÇA]"
                if quality == "verified"
                else "[MÉDIA]"
                if quality == "cited"
                else "[BAIXA — VERIFICAR]"
            )
            top_lines_list.append(
                f"{i+1}. {confidence_tag} {r.title or '(sem título)'} ({', '.join(s for s in r.sources if s)}) - score: {r.combined_score}\n   {(r.description or '')[:200]}..."
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
                "Prioritize sources marked with [ALTA CONFIANÇA] and handle with caution sources marked [BAIXA — VERIFICAR].\n\n"
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
        if not results:
            return (
                "No projects found for recommendation."
                if is_english
                else "Nenhum projeto encontrado para recomendacao."
            )
        top_lines_list = []
        for i, r in enumerate(results[:5]):
            quality = getattr(r, "evidence_quality", "unknown")
            confidence_tag = (
                "[ALTA CONFIANÇA]"
                if quality == "verified"
                else "[MÉDIA]"
                if quality == "cited"
                else "[BAIXA — VERIFICAR]"
            )
            top_lines_list.append(
                f"{i+1}. {confidence_tag} {r.title or '(sem título)'}\n   Pontos fortes: {', '.join(h for h in r.highlights if h)}\n   Metricas: {r.metrics}"
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
                "Give clear preference to [ALTA CONFIANÇA] projects. Avoid recommending [BAIXA — VERIFICAR] as primary option.\n\n"
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
                "Dê preferência clara aos projetos marcados com [ALTA CONFIANÇA]. Evite recomendar itens [BAIXA — VERIFICAR] como opção primária.\n\n"
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
            return f"Recomendamos **{top.title}** como principal opcao. {top.description[:200]}..."

    async def _generate_trends(
        self, results: list[SynthesizedResult], query: str | None = None
    ) -> str:
        """Identifica tendencias tecnologicas a partir dos resultados de pesquisa."""
        is_english = query is not None and self._is_query_english(query)
        if len(results) < 3:
            return (
                "Few data points for trends analysis."
                if is_english
                else "Poucos dados para analise de tendencias."
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
                else "Analise de tendencias nao disponivel."
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
    ) -> str:
        """Monta o relatorio final unindo todas as secoes geradas."""
        is_english = self._is_query_english(query)
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
                is_english=is_english,
            )
        )

        cleaned_lines = [str(line) for line in lines if line is not None]
        return "\n".join(cleaned_lines)

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

        exec_summary_clean = str(executive_summary or "").strip()
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
                f"# Relatorio: {query}",
                "",
                f"> Gerado em: {timestamp}  ",
                f"> Fontes pesquisadas: {', '.join(s for s in metadata.sources if s)}  ",
                f"> Resultados encontrados: {metadata.total_results}  ",
                f"> Iteracoes de pesquisa: {metadata.iterations}  ",
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
        else:
            lines = [
                "## 2. Projetos / Ferramentas Encontradas",
                "",
            ]

        for i, r in enumerate(results[:15]):
            metric_parts = []
            if "stars" in r.metrics:
                metric_parts.append(f"Stars: {r.metrics['stars']}")
            if "forks" in r.metrics:
                metric_parts.append(f"Forks: {r.metrics['forks']}")
            if "comments" in r.metrics:
                metric_parts.append(f"Comments: {r.metrics['comments']}")
            elif "upvotes" in r.metrics:
                metric_parts.append(f"Upvotes: {r.metrics['upvotes']}")
            if "updated_at" in r.metrics:
                metric_parts.append(f"Updated: {str(r.metrics['updated_at'])[:10]}")

            metrics_str = " | ".join(metric_parts)
            highlights_str = "\n".join(f"- {h}" for h in r.highlights if h) or (
                "- No specific highlights"
                if is_english
                else "- Nenhum destaque especifico"
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
                f"### 2.{i+1} {r.title or '(sem título)'}",
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
                    f"- **Descricao:** {desc_text}",
                    f"- **URLs:** {', '.join(u for u in r.urls[:3] if u)}",
                    f"- **Fontes:** {', '.join(s for s in r.sources if s)}",
                    f"- **Score:** {r.combined_score}/100",
                    f"- **Metricas:** {metrics_str}",
                    f"- **Highlights:**\n{highlights_str}",
                ]
                if next_step:
                    entry_lines.append(f"- **Proxima Acao:** {next_step}")
            entry_lines.append("")
            lines += entry_lines
        return lines

    def _build_analysis(
        self,
        results: list[SynthesizedResult],
        trends: str | None,
        recommendation: str | None,
        sentiment_section: str,
        metadata: ResearchMetadata,
        is_english: bool = False,
    ) -> list[str]:
        """Constroi o bloco de analise com tendencias, recomendacao e sentimento."""
        recommendation_clean = str(recommendation or "").strip()
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

        trends_clean = str(trends or "").strip()
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

        if is_english:
            lines = [
                "---",
                "",
                "## 3. Side-by-Side Comparison",
                "",
                "| Project | Stars | Forks | Updated | License | Score | Verdict |",
                "|---------|-------|-------|-------------|---------|-------|----------|",
            ]
        else:
            lines = [
                "---",
                "",
                "## 3. Comparacao Lado a Lado",
                "",
                "| Projeto | Stars | Forks | Atualizacao | Licenca | Score | Veredito |",
                "|---------|-------|-------|-------------|-------|----------|",
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

        if is_english:
            lines += [
                "",
                "---",
                "",
                "## 4. Identified Technologies / Stacks",
                "",
            ]
        else:
            lines += [
                "",
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
                "## 5. Discussao da Comunidade",
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
            lines += [
                "",
                "### Links Inválidos ou Quebrados Detectados",
                "As seguintes referências citadas pelas fontes originais falharam nos testes de conexão (404, timeouts ou inacessíveis):",
                "",
            ]
            for url in sorted(all_dead_links):
                lines.append(f"- ❌ {url}")

        if metadata.low_confidence_warnings:
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

        lines += [
            "",
            "---",
            "",
            f"*Relatório gerado por Smart Research Agent v2.0 | {metadata.timestamp.strftime('%Y-%m-%d %H:%M')}*",
        ]
        return lines

    def save_report(
        self,
        report: str,
        query: str,
        output_dir: str = "./reports",
        formats: list[ReportFormat] | None = None,
    ) -> str:
        """
        Salva o relatório no disco.

        Args:
            report: Conteúdo Markdown do relatório.
            query: Query original da pesquisa (usada para gerar o nome do arquivo).
            output_dir: Diretório de saída.
            formats: Lista de formatos adicionais a exportar além do Markdown padrão.
                     Exemplo: [ReportFormat.PDF, ReportFormat.DOCX]
                     Se None ou vazia, exporta apenas Markdown.

        Returns:
            Caminho do arquivo Markdown principal.
        """
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        import re
        import unicodedata

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
        logger.info(f"Relatorio salvo em: {md_path}")

        # ── Dispatcher de formatos adicionais ─────────────────────────────────────────────────
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

        return md_path
