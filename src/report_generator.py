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
                prompt = (
                    "Você é um analista sênior de inteligência tecnológica. Escreva em Português do Brasil.\n"
                    f"A seção '{header}' de um relatório técnico sobre '{query}' está vazia ou curta demais.\n"
                    "Gere uma análise técnica detalhada, aprofundada e formal (mínimo de 3 parágrafos robustos) para esta seção.\n\n"
                    f"Projetos encontrados como contexto:\n{project_summaries}\n\n"
                    f"Diretrizes específicas para a seção '{header}':\n"
                )

                if "Tecnologias / Stacks" in header:
                    prompt += (
                        "- Identifique as linguagens prováveis (ex: Rust, Python, TypeScript) e por que são usadas.\n"
                        "- Discorra sobre a arquitetura de transporte (HTTP, WebSockets, stdio) comumente empregada.\n"
                        "- Detalhe as dependências e ecossistemas envolvidos (ex: tokio, async-trait no Rust, fastmcp no Python)."
                    )
                elif "Discussao da Comunidade" in header or "Discussão" in header:
                    prompt += (
                        "- Sintetize a recepção geral desse tipo de tecnologia pela comunidade de desenvolvedores.\n"
                        "- Fale sobre os principais gargalos discutidos (curva de aprendizado, segurança de dados em LLMs).\n"
                        "- Cite o interesse observado através das estrelas e discussões gerais de adoção do protocolo MCP."
                    )
                elif "Sentimento" in header:
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
                self._generate_trends(results),
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
        if cached and all(cached.get(k) for k in ("executive_summary", "recommendation", "trends")):
            logger.info("ReportGenerator: secoes narrativas recuperadas do cache.")
            return cached

        try:
            sections = await self._generate_sections_consolidated(query, results, metadata)
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

        data = await self.llm.generate_structured(prompt, _SECTIONS_SCHEMA, temperature=0.35)

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
        """Gera o resumo executivo da pesquisa usando o LLM.

        Args:
            query: Query original do usuario.
            results: Resultados sintetizados para contexto do LLM.
            metadata: Metadados da pesquisa incluindo fontes e duracao.

        Returns:
            str: Paragrafos de resumo executivo gerados pelo LLM,
                 ou fallback textual em caso de erro.
        """
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
            return (
                f"Pesquisa sobre '{query}' encontrou {len(results)} projetos relevantes "
                f"em {', '.join(s for s in metadata.sources if s)}."
            )

    async def _generate_recommendation(
        self,
        query: str,
        results: list[SynthesizedResult],
    ) -> str:
        """Gera uma recomendacao estrategica baseada nos resultados da pesquisa.

        Args:
            query: Query original do usuario.
            results: Resultados sintetizados para contexto do LLM.

        Returns:
            str: Recomendacao acionavel gerada pelo LLM,
                 ou recomendacao automatica baseada nos scores em caso de erro.
        """
        if not results:
            return "Nenhum projeto encontrado para recomendacao."
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
            return f"Recomendamos **{top.title}** como principal opcao. {top.description[:200]}..."

    async def _generate_trends(self, results: list[SynthesizedResult]) -> str:
        """Identifica tendencias tecnologicas a partir dos resultados de pesquisa.

        Usa o LLM para extrair 2-3 tendencias com evidencias concretas dos projetos.

        Args:
            results: Resultados sintetizados com titulos e descricoes dos projetos.

        Returns:
            str: Bloco de tendencias em Markdown gerado pelo LLM,
                 ou mensagem de fallback se dados forem insuficientes ou LLM falhar.
        """
        if len(results) < 3:
            return "Poucos dados para analise de tendencias."
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
            logger.warning(f"LLM trends falhou: {e}")
            return "Analise de tendencias nao disponivel."

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
        """Monta o relatorio final unindo todas as secoes geradas.

        Delega a construcao de cada bloco para os submétodos privados
        `_build_summary`, `_build_sources` e `_build_analysis`.

        Args:
            query: Query original do usuario.
            metadata: Metadados da pesquisa.
            results: Resultados sintetizados.
            executive_summary: Texto do resumo executivo gerado pelo LLM.
            recommendation: Texto da recomendacao gerado pelo LLM.
            trends: Texto das tendencias gerado pelo LLM.
            timeline_section: Bloco Markdown de timeline cronologica.
            sentiment_section: Bloco Markdown de analise de sentimento.
            comparison_section: Bloco Markdown de comparacao de alternativas.

        Returns:
            str: Relatorio completo em formato Markdown.
        """
        lines = []
        lines.extend(
            self._build_summary(
                query,
                metadata,
                executive_summary,
                comparison_section,
                timeline_section,
                results,
            )
        )
        lines.extend(self._build_sources(results))
        lines.extend(
            self._build_analysis(
                results, trends, recommendation, sentiment_section, metadata
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
    ) -> list[str]:
        """Constroi o bloco de cabecalho e resumo executivo do relatorio.

        Args:
            query: Query original do usuario.
            metadata: Metadados da pesquisa (timestamp, fontes, etc).
            executive_summary: Texto do resumo executivo.
            comparison_section: Bloco Markdown de comparacao.
            timeline_section: Bloco Markdown de timeline.
            results: Resultados sintetizados para fallback de resumo.

        Returns:
            list[str]: Linhas Markdown do bloco de resumo.
        """
        timestamp = metadata.timestamp.strftime("%Y-%m-%d %H:%M")

        exec_summary_clean = str(executive_summary or "").strip()
        if not exec_summary_clean:
            exec_summary_clean = (
                f"Pesquisa realizada com sucesso sobre '{query}'. Foram encontrados {len(results)} "
                f"projetos relevantes nas fontes pesquisadas ({', '.join(s for s in metadata.sources if s)}). "
                f"Consulte a lista de ferramentas detalhadas abaixo para obter mais informações."
            )

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
            "",
        ]

        if comparison_section:
            lines += [
                comparison_section,
                "",
                "---",
                "",
            ]

        if timeline_section:
            lines += [
                timeline_section,
                "",
                "---",
                "",
            ]
        return lines

    def _build_sources(self, results: list[SynthesizedResult]) -> list[str]:
        """Constroi o bloco de listagem de projetos e ferramentas encontradas.

        Formata cada resultado sintetizado com metricas, veredicto, TL;DR,
        alertas de qualidade e proxima acao recomendada.

        Args:
            results: Lista de `SynthesizedResult` para exibir (max 15).

        Returns:
            list[str]: Linhas Markdown da secao de projetos e ferramentas.
        """
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
            highlights_str = (
                "\n".join(f"- {h}" for h in r.highlights if h)
                or "- Nenhum destaque especifico"
            )
            desc_text = (r.description or "")[:300] + (
                "..." if len(r.description or "") > 300 else ""
            )

            verdict = getattr(r, "verdict", "") or ""
            tldr = getattr(r, "tldr", "") or ""
            next_step = getattr(r, "next_step", "") or ""
            read_min = getattr(r, "read_min", 0) or 0

            verdict_icons = {
                "Foca": "🔴 Foca",
                "Considera": "🟡 Considera",
                "Acompanha": "🟢 Acompanha",
                "Ignora": "⚪ Ignora",
            }
            verdict_display = verdict_icons.get(verdict, verdict)

            evidence_quality = getattr(r, "evidence_quality", "unknown")
            quality_badges = {
                "verified": "🌟 Verificado (Alta Confiança)",
                "cited": "📖 Citado (Confiança Média)",
                "inferred": "🔍 Inferido (Confiança Baixa)",
                "unknown": "❓ Desconhecido",
            }
            quality_display = quality_badges.get(evidence_quality, evidence_quality)

            is_single_source = len(r.sources) <= 1
            source_warning = (
                " | ⚠️ **Fonte Única (Single Source)**" if is_single_source else ""
            )

            flags = getattr(r, "hallucination_flags", []) or []
            flags_display = ""
            if flags:
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
                entry_lines.append(
                    f"> **Veredito:** {verdict_display}  |  ⏱️ ~{read_min} min  |  **Qualidade:** {quality_display}{source_warning}{flags_display}"
                )
            if tldr:
                entry_lines.append(f"> {tldr}")
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
    ) -> list[str]:
        """Constroi o bloco de analise com tendencias, recomendacao e sentimento.

        Args:
            results: Resultados sintetizados para contexto da analise.
            trends: Texto de tendencias gerado pelo LLM.
            recommendation: Texto de recomendacao gerado pelo LLM.
            sentiment_section: Bloco Markdown de analise de sentimento.
            metadata: Metadados da pesquisa.

        Returns:
            list[str]: Linhas Markdown do bloco de analise.
        """
        recommendation_clean = str(recommendation or "").strip()
        if not recommendation_clean:
            recommendation_clean = (
                "### Recomendação Automática\n"
                "Com base nos dados disponíveis, sugerimos priorizar os projetos com maiores pontuações de relevância "
                "e atividade contínua no repositório. Verifique a tabela de comparação para detalhes adicionais."
            )

        trends_clean = str(trends or "").strip()
        if not trends_clean:
            trends_clean = (
                "- **Foco em Integração Simplificada**: Crescimento de ferramentas prontas e CDNs.\n"
                "- **Segurança e Privacidade**: Foco em soluções self-hosted e políticas de RLS/mTLS."
            )

        lines = [
            "---",
            "",
            "## 3. Comparacao Lado a Lado",
            "",
            "| Projeto | Stars | Forks | Atualizacao | Licenca | Score | Veredito |",
            "|---------|-------|-------|-------------|---------|-------|----------|",
        ]
        for r in results[:10]:
            stars = r.metrics.get("stars", "-")
            forks = r.metrics.get("forks", "-")
            updated = str(r.metrics.get("updated_at", "-"))[:10]
            license_id = r.metrics.get("license", "-")
            verdict = getattr(r, "verdict", "") or "-"
            lines.append(
                f"| {(r.title or '')[:30]} | {stars} | {forks} | {updated} | {license_id} | {r.combined_score} | {verdict} |"
            )

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
            lines.append(f"- **{lang}** — usado por {proj_str}")

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
        all_urls: list = []
        for r in results[:20]:
            for url in r.urls:
                if url not in all_urls:
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
        slug = query.lower().replace(" ", "-").replace("/", "-")[:50]
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
