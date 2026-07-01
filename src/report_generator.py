from typing import List, Optional
from datetime import datetime
from pathlib import Path
import os
from src.types import SynthesizedResult, ResearchMetadata, ReportFormat
from src.clients.llm_client import LLMClient
from src.temporal_analyzer import TemporalAnalyzer
from src.sentiment_analyzer import SentimentAnalyzer
from src.comparator import Comparator
import logging

logger = logging.getLogger(__name__)


class ReportGenerator:
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
        self.temporal_analyzer = TemporalAnalyzer()
        self.sentiment_analyzer = SentimentAnalyzer()
        self.comparator = Comparator()

    async def generate(
        self,
        query: str,
        results: List[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> str:
        executive_summary = await self._generate_executive_summary(query, results, metadata)
        recommendation = await self._generate_recommendation(query, results)
        trends = await self._generate_trends(results)
        timeline_section = self.temporal_analyzer.generate_timeline_section(results)
        sentiment_section = self.sentiment_analyzer.generate_sentiment_section(results)
        comparison_section = self.comparator.generate_comparison_section(query, results)

        return self._assemble_report(
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

    async def _generate_executive_summary(
        self,
        query: str,
        results: List[SynthesizedResult],
        metadata: ResearchMetadata,
    ) -> str:
        top_lines_list = []
        for i, r in enumerate(results[:5]):
            quality = getattr(r, "evidence_quality", "unknown")
            confidence_tag = "[ALTA CONFIANÇA]" if quality == "verified" else "[MÉDIA]" if quality == "cited" else "[BAIXA — VERIFICAR]"
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
        self, query: str, results: List[SynthesizedResult]
    ) -> str:
        if not results:
            return "Nenhum projeto encontrado para recomendacao."
        top_lines_list = []
        for i, r in enumerate(results[:5]):
            quality = getattr(r, "evidence_quality", "unknown")
            confidence_tag = "[ALTA CONFIANÇA]" if quality == "verified" else "[MÉDIA]" if quality == "cited" else "[BAIXA — VERIFICAR]"
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

    async def _generate_trends(self, results: List[SynthesizedResult]) -> str:
        if len(results) < 3:
            return "Poucos dados para analise de tendencias."
        project_lines = "\n".join(
            f"- {r.title or '(sem título)'}: {(r.description or '')[:150]}..." for r in results[:8]
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
        results: List[SynthesizedResult],
        executive_summary: Optional[str],
        recommendation: Optional[str],
        trends: Optional[str],
        timeline_section: str = "",
        sentiment_section: str = "",
        comparison_section: str = "",
    ) -> str:
        timestamp = metadata.timestamp.strftime("%Y-%m-%d %H:%M")

        exec_summary_clean = str(executive_summary or "").strip()
        if not exec_summary_clean:
            exec_summary_clean = (
                f"Pesquisa realizada com sucesso sobre '{query}'. Foram encontrados {len(results)} "
                f"projetos relevantes nas fontes pesquisadas ({', '.join(s for s in metadata.sources if s)}). "
                f"Consulte a lista de ferramentas detalhadas abaixo para obter mais informações."
            )

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

        lines += [
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
            highlights_str = "\n".join(f"- {h}" for h in r.highlights if h) or "- Nenhum destaque especifico"
            desc_text = (r.description or "")[:300] + ("..." if len(r.description or "") > 300 else "")

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

            # Novo V2: Qualidade de evidência e avisos de confiança
            evidence_quality = getattr(r, "evidence_quality", "unknown")
            quality_badges = {
                "verified": "🌟 Verificado (Alta Confiança)",
                "cited": "📖 Citado (Confiança Média)",
                "inferred": "🔍 Inferido (Confiança Baixa)",
                "unknown": "❓ Desconhecido"
            }
            quality_display = quality_badges.get(evidence_quality, evidence_quality)

            # Alerta de Fonte Única (Single Source)
            is_single_source = len(r.sources) <= 1
            source_warning = " | ⚠️ **Fonte Única (Single Source)**" if is_single_source else ""

            # Flags de alucinação/confiança
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
                    "absolute_claim_detected": "Afirmação Absoluta"
                }
                flags_display = " | 🚫 **Alertas:** " + ", ".join(flag_labels.get(f, f) for f in flags)

            entry_lines = [
                f"### 2.{i+1} {r.title or '(sem título)'}",
            ]
            if verdict_display:
                entry_lines.append(f"> **Veredito:** {verdict_display}  |  ⏱️ ~{read_min} min  |  **Qualidade:** {quality_display}{source_warning}{flags_display}")
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

        lines += [
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
        for lang, projects in sorted(languages.items(), key=lambda x: len(x[1]), reverse=True)[:10]:
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
                lines.append(f"- **r/{sub}**: {(r.title or '')[:80]}... ({upvotes} upvotes)")
            lines.append("")
        if hn_results:
            lines.append("### Hacker News")
            for r in hn_results[:3]:
                points = r.metrics.get("points", 0)
                author = r.metrics.get("author", "unknown")
                lines.append(f"- **{author}**: {(r.title or '')[:80]}... ({points} points)")
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

        # Novo V2: Agrega todos os links mortos para transparência
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
                ""
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

        cleaned_lines = [str(line) for line in lines if line is not None]
        return "\n".join(cleaned_lines)

    def save_report(
        self,
        report: str,
        query: str,
        output_dir: str = "./reports",
        formats: Optional[List[ReportFormat]] = None,
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
