"""Interface Web Interativa do Smart Research Agent (Streamlit).

Refatorada para o SRA Upgrade v6.2.0 com visualização melhorada do EvidenceGraph.
"""

from __future__ import annotations

import streamlit as st
import asyncio
import json
import time
from datetime import datetime

# ── Bloco 12 (E5-T1): Dashboard de Qualidade em Tempo Real ──────────────────


def _quality_gauge(label: str, value: float | None, threshold: float) -> None:
    """Renderiza um medidor de qualidade colorido (verde/amarelo/vermelho)."""
    if value is None:
        st.metric(label, "N/A")
        return
    pct = value * 100
    if value >= threshold:
        color, emoji = "#22c55e", "🟢"
    elif value >= threshold * 0.8:
        color, emoji = "#f59e0b", "🟡"
    else:
        color, emoji = "#ef4444", "🔴"
    st.markdown(
        f"<div style='border-left:4px solid {color};padding:0.5rem 1rem;"
        f"background:#f8fafc;border-radius:8px;margin-bottom:0.5rem;'>"
        f"<div style='font-size:0.8rem;color:#64748b;'>{label}</div>"
        f"<div style='font-size:1.6rem;font-weight:700;color:{color};'>"
        f"{emoji} {pct:.0f}%</div></div>",
        unsafe_allow_html=True,
    )


def render_quality_dashboard(ctx) -> None:
    """Painel 'Qualidade desta pesquisa' (Bloco 12) com breakdown RAGAS.

    Lê ``quality_gate_result`` de ``context.extra`` (produzido pelo QualityGate
    do Bloco 6 / E1-T2) e exibe faithfulness/relevancy/traceability com cores.
    """
    if ctx is None:
        return
    qg = ctx.extra.get("quality_gate_result") if hasattr(ctx, "extra") else None
    if not qg:
        st.info("⚠️ Quality Gate (RAGAS) não produziu scores para esta pesquisa.")
        return
    st.markdown("### 🧪 Qualidade desta pesquisa (RAGAS)")
    c1, c2, c3 = st.columns(3)
    with c1:
        _quality_gauge("Faithfulness", getattr(qg, "faithfulness", None), 0.70)
    with c2:
        _quality_gauge("Relevancy", getattr(qg, "relevancy", None), 0.75)
    with c3:
        _quality_gauge("Traceability", getattr(qg, "traceability", None), 0.80)
    mode = getattr(qg, "mode", "proxy")
    verdict = "✅ Aprovado" if getattr(qg, "passed", False) else "⚠️ Abaixo do limiar"
    st.caption(
        f"Modo de avaliação: **{mode}** · Veredito: {verdict} "
        f"· Retry recomendado: "
        f"{'sim' if getattr(qg, 'retry_recommended', False) else 'não'}"
    )


def export_report_markdown(report_md: str, query: str) -> None:
    """Botão de download .md nativo (sem dependências externas)."""
    import re as _re

    safe = _re.sub(r"[^\w\-]+", "_", (query or "relatorio"))[:40]
    st.download_button(
        "📝 Baixar .md",
        report_md,
        file_name=f"sra_{safe}.md",
        mime="text/markdown",
    )


def export_report_pdf_docx(report_md: str, query: str) -> None:
    """Botões .pdf e .docx usando libs já instaladas no venv (guarded)."""
    import re as _re
    from io import BytesIO

    safe = _re.sub(r"[^\w\-]+", "_", (query or "relatorio"))[:40]
    col_p, col_d = st.columns(2)
    with col_p:
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib.units import cm
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

            buf = BytesIO()
            doc = SimpleDocTemplate(
                buf, pagesize=A4, topMargin=2 * cm, bottomMargin=2 * cm
            )
            styles = getSampleStyleSheet()
            flow = []
            for line in report_md.splitlines():
                flow.append(Paragraph((line or "&nbsp;")[:2000], styles["Normal"]))
                flow.append(Spacer(1, 4))
            doc.build(flow)
            st.download_button(
                "📄 Baixar .pdf",
                buf.getvalue(),
                file_name=f"sra_{safe}.pdf",
                mime="application/pdf",
            )
        except Exception as e:  # pragma: no cover - graceful sem lib
            st.caption(f"Export .pdf indisponível: {e}")
    with col_d:
        try:
            from docx import Document

            d = Document()
            for line in report_md.splitlines():
                d.add_paragraph(line)
            buf = BytesIO()
            d.save(buf)
            st.download_button(
                "📊 Baixar .docx",
                buf.getvalue(),
                file_name=f"sra_{safe}.docx",
                mime=(
                    "application/vnd.openxmlformats-"
                    "officedocument.wordprocessingml.document"
                ),
            )
        except Exception as e:  # pragma: no cover - graceful sem lib
            st.caption(f"Export .docx indisponível: {e}")


# Configuração da página Streamlit com estética premium
st.set_page_config(
    page_title="Smart Research Agent Studio v6.2.0",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Estilo CSS customizado para WOW imediato (Fontes premium, cards, sombras, gradientes)
st.markdown(
    """
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap');

        html, body, [class*="css"] {
            font-family: 'Outfit', sans-serif;
        }

        /* Top bar com gradiente premium */
        .top-bar {
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            padding: 2rem;
            border-radius: 12px;
            color: white;
            margin-bottom: 2rem;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
        }
        .top-bar h1 {
            margin: 0;
            font-size: 2.5rem;
            font-weight: 700;
            letter-spacing: -0.5px;
        }
        .top-bar p {
            margin: 0.5rem 0 0 0;
            opacity: 0.9;
            font-size: 1.1rem;
        }

        /* Cards de Agentes do Swarm */
        .agent-card {
            background-color: #ffffff;
            border-left: 5px solid #2a5298;
            padding: 1.5rem;
            border-radius: 8px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05);
            margin-bottom: 1rem;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .agent-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 12px rgba(0, 0, 0, 0.10);
        }
        .agent-name {
            font-size: 1.2rem;
            font-weight: 600;
            color: #1e3c72;
            margin-bottom: 0.5rem;
        }
        .agent-status {
            display: inline-block;
            padding: 0.25rem 0.6rem;
            border-radius: 12px;
            font-size: 0.8rem;
            font-weight: 600;
            text-transform: uppercase;
        }
        .status-idle { background-color: #e2e8f0; color: #4a5568; }
        .status-active {
            background-color: #c6f6d5;
            color: #22543d;
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0% { opacity: 0.7; }
            50% { opacity: 1; }
            100% { opacity: 0.7; }
        }

        /* Estilo da aba do grafo */
        .graph-container {
            border: 2px solid #e2e8f0;
            border-radius: 12px;
            background-color: #ffffff;
            padding: 1.5rem;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
        }

        .graph-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
            padding-bottom: 1rem;
            border-bottom: 2px solid #e2e8f0;
        }

        .graph-title {
            font-size: 1.5rem;
            font-weight: 700;
            color: #1e3c72;
        }

        .graph-stats {
            display: flex;
            gap: 2rem;
            font-size: 0.9rem;
            color: #718096;
        }

        .stat-item {
            display: flex;
            flex-direction: column;
            align-items: center;
        }

        .stat-value {
            font-size: 1.3rem;
            font-weight: 600;
            color: #2a5298;
        }

        .stat-label {
            font-size: 0.8rem;
            color: #718096;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# Renderização do cabeçalho
st.markdown(
    """
    <div class="top-bar">
        <h1>🛡️ Smart Research Agent Studio <span style="font-size:1.2rem; vertical-align:middle; background:#00c6ff; padding:2px 8px; border-radius:4px;">v6.2.0</span></h1>
        <p>Plataforma de pesquisa corporativa com análise de grafos em tempo real (EvidenceGraph) e insights baseados em AI</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# Sidebar de Configurações
with st.sidebar:
    st.markdown("### ⚙️ Painel de Controle")
    mode = st.selectbox(
        "Modo de Operação",
        [
            "cirurgia",
            "guerrilha",
            "radar",
            "arqueologia",
            "concorrencia",
            "black_ops",
            "debate",
            "mito",
        ],
        help=(
            "guerrilha=rápido, cirurgia=preciso com auditoria, "
            "black_ops=pesquisa profunda, "
            "debate=motor multi-agente (hipóteses opostas + juiz LLM), "
            "mito=fact-checking de mitos populares (web/Wikipedia/Snopes/Reddit)"
        ),
    )
    max_results = st.slider("Resultados máximos por fonte", 3, 25, 12)
    languages = st.multiselect(
        "Idiomas de Busca", ["en", "pt", "es", "zh"], default=["en", "pt"]
    )
    st.divider()
    st.markdown("### 🧬 Recursos Avançados")
    st.checkbox("Habilitar Docker Sandbox", value=True)
    st.checkbox("Habilitar GraphRAG (KuzuDB)", value=True)
    st.checkbox("Habilitar Análise de Contradições", value=True)
    st.checkbox("Chat com a Pesquisa (RAG)", value=True)
    st.checkbox("Data Analysis com Pandas", value=True)
    st.checkbox("Exportação de Citações (BibTeX/RIS)", value=True)
    st.divider()
    st.markdown("### 🎯 Fontes de Confiança")
    with st.expander("Gerenciar regras de fonte", expanded=False):
        col_source, col_tier, col_add = st.columns([3, 2, 1])
        with col_source:
            new_source = st.text_input(
                "Fonte",
                placeholder="ex: reddit ou blog-duvidoso.com",
                label_visibility="collapsed",
                key="trust_new_source",
            )
        with col_tier:
            new_tier = st.selectbox(
                "Regra",
                options=["allow", "deny"],
                format_func=lambda x: (
                    "✅ Sempre priorizar" if x == "allow" else "🚫 Nunca mostrar"
                ),
                label_visibility="collapsed",
                key="trust_new_tier",
            )
        with col_add:
            if st.button("➕", use_container_width=True, key="trust_add_btn"):
                if new_source:
                    rules = st.session_state.get("trust_rules", {})
                    rules[new_source.strip().lower()] = new_tier
                    st.session_state["trust_rules"] = rules
                    st.rerun()

        # Tabela das regras ativas
        rules = st.session_state.get("trust_rules", {})
        if rules:
            st.markdown("**Regras ativas:**")
            for source, tier in list(rules.items()):
                c1, c2, c3 = st.columns([3, 2, 1])
                c1.write(f"`{source}`")
                c2.write("✅ Prioridade" if tier == "allow" else "🚫 Bloqueado")
                if c3.button("🗑️", key=f"del_rule_{source}"):
                    del rules[source]
                    st.session_state["trust_rules"] = rules
                    st.rerun()
        else:
            st.caption(
                "Nenhuma regra configurada — todas as fontes são tratadas igualmente."
            )
    st.divider()
    st.info(
        "SRA v6.2.0 — Super Ferramenta de Pesquisa completa com suporte a EvidenceGraph."
    )

# Abas de navegação principal (Lab Mode)
tab_search, tab_swarm, tab_gate, tab_graph = st.tabs(
    [
        "🔎 Console de Pesquisa",
        "🤖 Chat com a Pesquisa",
        "🚦 Gatekeeping (HITL)",
        "📊 EvidenceGraph (Grafo de Conhecimento)",
    ]
)

# ---- ABA 1: CONSOLE DE PESQUISA ----
with tab_search:
    st.markdown("### 🚀 Iniciar Nova Pesquisa")
    query = st.text_input(
        "O que você deseja pesquisar hoje?",
        placeholder="Ex: benchmarks de performance do python 3.12 em produção",
        key="query_input",
    )

    col_btn_run, col_btn_clear, _ = st.columns([1.5, 1, 5])
    with col_btn_run:
        run_btn = st.button(
            "🚀 Executar Swarm", type="primary", use_container_width=True
        )
    with col_btn_clear:
        clear_btn = st.button("🗑️ Limpar", use_container_width=True)

    if run_btn and query:
        with st.spinner("Orquestrando agentes do Swarm..."):
            status_bar = st.progress(0)
            status_text = st.empty()
            live_feed = st.empty()

            try:
                from src.config import Config
                from src.orchestrator import Orchestrator

                config = Config()
                config.operation_mode = mode
                config.max_results_per_source = max_results

                orch = Orchestrator(config)
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                # FASE 5: repassa regras de allowlist/denylist para o pipeline
                trust_rules = st.session_state.get("trust_rules", {})

                # ── Bloco 12: Progress tracker por fase real do pipeline ──
                _PHASES = [
                    (
                        "🧬 Intent Analyzer",
                        "Analisando intenção e extraindo conceitos...",
                    ),
                    ("🔍 Query Expander", "Expandindo sub-queries complementares..."),
                    (
                        "🌐 Search + Rank",
                        "Executando buscas paralelas e ranqueando fontes...",
                    ),
                    ("🧩 Synthesis", "Sintetizando entidades e agrupando clusters..."),
                    ("🧪 Quality Gate", "Avaliando faithfulness/relevancy (RAGAS)..."),
                    ("📋 Report", "Gerando relatório de síntese final..."),
                ]

                # Avança o progresso fase a fase enquanto a orquestração roda.
                # Como o orchestrator roda sincronamente, exibimos o feed de fases
                # e atualizamos o contador de fontes a partir do contexto retornado.
                status_text.info(f"{_PHASES[0][0]} — {_PHASES[0][1]}")
                status_bar.progress(round(1 / len(_PHASES) * 100))

                # Roda a orquestração assíncrona
                result = loop.run_until_complete(
                    orch.research(query, context_extra={"trust_rules": trust_rules})
                )
                ctx = getattr(orch, "last_context", None)

                # Live feed (Bloco 12): fontes consultadas conforme retornam.
                ranked = (getattr(ctx, "ranked_results", None) or []) if ctx else []
                src_count: dict = {}
                for r in ranked:
                    s = getattr(r, "source", "desconhecida")
                    src_count[s] = src_count.get(s, 0) + 1
                if src_count:
                    feed_lines = " · ".join(
                        f"**{s}** ({n})"
                        for s, n in sorted(
                            src_count.items(), key=lambda x: x[1], reverse=True
                        )
                    )
                    live_feed.markdown(
                        f"🌐 Fontes consultadas: {feed_lines} — "
                        f"{len(ranked)} resultados ranqueados"
                    )
                else:
                    live_feed.caption(
                        "🌐 Nenhum resultado ranqueado retornado nesta pesquisa."
                    )

                # Salva a instância do orquestrador e contexto para uso em outras abas (ex: Grafo)
                st.session_state["orch"] = orch
                st.session_state["orch_context"] = ctx
                st.session_state["orch_result"] = (
                    result if isinstance(result, str) else json.dumps(result, indent=2)
                )

                for i, (name, desc) in enumerate(_PHASES[1:], start=2):
                    status_text.info(f"{name} — {desc}")
                    status_bar.progress(round(i / len(_PHASES) * 100))

                status_bar.progress(100)
                status_text.success("Orquestração concluída!")

                st.markdown("---")
                st.markdown("### 📋 Relatório de Síntese")
                st.markdown(
                    result if isinstance(result, str) else json.dumps(result, indent=2)
                )

                # ── Bloco 12: Painel de Qualidade (RAGAS) + Exportação ──
                render_quality_dashboard(ctx)

                report_md = st.session_state.get("orch_result", "") or ""
                if report_md:
                    st.divider()
                    st.markdown("### 📤 Exportar Relatório")
                    export_report_markdown(report_md, query)
                    export_report_pdf_docx(report_md, query)

                # FASE 5: Painel de Transparência da Busca
                with st.expander("🔍 Transparência da busca", expanded=False):
                    ctx = st.session_state.get("orch_context")
                    ranked = []
                    if ctx is not None:
                        ranked = getattr(ctx, "ranked_results", None) or []

                    if ranked:
                        st.markdown("**Fontes consultadas:**")
                        sources_used: dict = {}
                        for r in ranked:
                            source = getattr(r, "source", "desconhecida")
                            trust = getattr(r, "trust_tier", "neutral")
                            if source not in sources_used:
                                sources_used[source] = {
                                    "count": 0,
                                    "trust": trust,
                                    "avg_confidence": [],
                                }
                            sources_used[source]["count"] += 1
                            confidence = getattr(r, "confidence_score", None)
                            if confidence:
                                sources_used[source]["avg_confidence"].append(
                                    confidence
                                )

                        for src, info in sorted(
                            sources_used.items(),
                            key=lambda x: x[1]["count"],
                            reverse=True,
                        ):
                            avg_conf = (
                                sum(info["avg_confidence"])
                                / len(info["avg_confidence"])
                                if info["avg_confidence"]
                                else None
                            )
                            trust_emoji = {
                                "allow": "✅",
                                "deny": "🚫",
                                "neutral": "⚪",
                            }.get(info["trust"], "⚪")
                            conf_str = f"{avg_conf:.0%}" if avg_conf else "N/A"
                            st.markdown(
                                f"- {trust_emoji} **{src}**: {info['count']} "
                                f"resultado(s) | Confiança média: {conf_str}"
                            )

                        # FASE 5 — Badges de tom/sentimento por resultado (GDELT).
                        # Mostra um badge visual indicando o tom da cobertura de
                        # cada notícia quando disponível (metrics.tone).
                        st.divider()
                        st.markdown("**🌈 Tom / Sentimento das notícias:**")
                        tone_shown = 0
                        for r in ranked:
                            metrics = getattr(r, "metrics", {}) or {}
                            tone = metrics.get("tone")
                            title = getattr(r, "title", "") or "(sem título)"
                            if isinstance(tone, (int, float)):
                                if tone > 2.0:
                                    badge = "🟢 **Positivo**"
                                elif tone < -2.0:
                                    badge = "🔴 **Crítico**"
                                else:
                                    badge = "⚪ **Neutro**"
                                st.markdown(f"- {badge} `{tone:+.2f}` — {title[:80]}")
                                tone_shown += 1
                        if tone_shown == 0:
                            st.caption(
                                "Nenhum dado de tom disponível (fontes sem sinal "
                                "de sentimento do GDELT)."
                            )
                    else:
                        st.caption("Nenhum resultado ranqueado disponível para exibir.")

                    # Custo estimado (se disponível no contexto)
                    cost = None
                    if ctx is not None:
                        cost = (
                            ctx.extra.get("estimated_cost_usd")
                            if hasattr(ctx, "extra")
                            else None
                        )
                    if cost:
                        st.metric("Custo estimado (USD)", f"~${float(cost):.4f}")

                    st.markdown("**Fontes com falha (circuit breaker):**")
                    st.caption("Ver `/api/circuit-breakers` para detalhes completos.")

            except Exception as e:
                st.error(f"Erro no pipeline: {e}")

# ---- ABA 2: CHAT COM A PESQUISA ----
with tab_swarm:
    st.markdown("### 🤖 Chat com a Pesquisa (RAG pós-relatório)")
    st.write(
        "Faça perguntas de acompanhamento sobre o relatório do grafo de conhecimento, "
        "baseado em dados e evidências já coletadas."
    )

    _orch = st.session_state.get("orch")

    # Histórico de chat simples
    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []

    # Exibe histórico
    for msg in st.session_state["chat_history"]:
        if msg["role"] == "user":
            st.markdown(f"**Você:** {msg['content']}")
        else:
            st.markdown(f"**Assistente:** {msg['content']}")

    # Área de entrada
    user_question = st.chat_input("Faça uma pergunta sobre a pesquisa...")

    if user_question:
        # Adiciona mensagem do usuário
        st.session_state["chat_history"].append(
            {"role": "user", "content": user_question}
        )

        # Simula uma resposta baseada no contexto do orquestrador
        if _orch:
            try:
                from src.chat_session import ChatSession

                chat = ChatSession(orchestrator=_orch)
                # Requer método start_session com session_id, mas temos orquestrador
                chat.start_session(
                    session_id="streamlit_" + str(hash(user_question)),
                    query=user_question,
                    context={
                        "report": st.session_state.get("orch_result", ""),
                        "evidence_graph": getattr(_orch, "evidence_graph", None),
                        "ranked_results": getattr(_orch, "ranked_results", [])
                        if hasattr(_orch, "ranked_results")
                        else [],
                    },
                )

                answer = chat.ask(user_question)

                st.session_state["chat_history"].append(
                    {"role": "assistant", "content": answer}
                )

                # Atualiza o orquestrador no estado de sessão se resultado estiver disponível
                if hasattr(_orch, "research"):
                    # Guarda resultado do orquestrador para uso posterior
                    if "orch_result" not in st.session_state:
                        try:
                            loop = asyncio.new_event_loop()
                            st.session_state["orch_result"] = loop.run_until_complete(
                                _orch.research(user_question)
                            )
                        except Exception:
                            st.session_state["orch_result"] = (
                                "(Relatório de pesquisa não disponível)"
                            )
                    else:
                        pass

            except Exception as e:
                st.error(f"Erro no chat RAG: {e}")

        else:
            st.error("Nenhum orquestrador carregado. Execute uma pesquisa primeiro.")

# ---- ABA 3: GATEKEEPING (HITL) ----
with tab_gate:
    st.markdown("### 🚦 Central de Gatekeeping (Human-in-the-Loop)")
    st.write(
        "Aprovação manual e refino de consultas planejadas antes de disparar buscas:"
    )

    # Card Simulado de aprovação
    st.info("Outlines pendentes de revisão:")
    st.markdown(
        """
        <div style="background-color:#fffdf5; border:1px solid #f6ad55; padding:1.5rem; border-radius:8px;">
            <strong style="color:#dd6b20;">Tópico:</strong> Python 3.12 Performance Benchmarks<br>
            <strong style="color:#dd6b20;">Queries Propostas:</strong>
            <ul>
                <li>"python 3.12 interpreter loop speed improvement"</li>
                <li>"PEP 709 inline comprehensions benchmark"</li>
                <li>"python 3.12 vs 3.11 memory consumption wsl2"</li>
            </ul>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.write("")
    col_app, col_rej = st.columns([1, 4])
    with col_app:
        st.button("✅ Aprovar Outline", type="primary")
    with col_rej:
        st.button("❌ Rejeitar e Regenerar")

# ---- ABA 4: EVIDENCEGRAPH (GRAFO DE CONHECIMENTO) ----
with tab_graph:
    st.markdown("### 📊 EvidenceGraph - Visualização Interativa D3.js")

    _orch = st.session_state.get("orch")
    _eg = getattr(_orch, "evidence_graph", None) if _orch else None

    if _eg and hasattr(_eg, "export_d3_json") and callable(_eg.export_d3_json):
        import json as _json
        import streamlit.components.v1 as _components

        try:
            _d3_data = _eg.export_d3_json()
            _d3_json_str = _json.dumps(_d3_data)

            _html = f"""
<!DOCTYPE html>
<html>
<head>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  body {{ margin: 0; background: #f7fafc; font-family: sans-serif; overflow: hidden; }}
  .link {{ stroke: #a0aec0; stroke-opacity: 0.5; stroke-width: 1.5px; }}
  .node {{ stroke: #fff; stroke-width: 1.5px; cursor: pointer; }}
  .label {{ font-size: 10px; fill: #2d3748; pointer-events: none; }}
  .tooltip {{
    position: absolute; padding: 8px 10px;
    background: rgba(255,255,255,0.96); border: 1px solid #cbd5e0;
    border-radius: 6px; font-size: 11px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.08);
    pointer-events: none; max-width: 280px; z-index: 9999;
  }}
</style>
</head>
<body>
<div id="graph"></div>
<script>
const data = {_d3_json_str};
const W = window.innerWidth || 800, H = 500;

const svg = d3.select("#graph").append("svg")
  .attr("width", W).attr("height", H)
  .call(d3.zoom().on("zoom", e => g.attr("transform", e.transform)));

const g = svg.append("g");

const sim = d3.forceSimulation(data.nodes)
  .force("link", d3.forceLink(data.links).id(d => d.id).distance(110))
  .force("charge", d3.forceManyBody().strength(-180))
  .force("center", d3.forceCenter(W / 2, H / 2))
  .force("collide", d3.forceCollide(20));

const link = g.append("g").selectAll("line")
  .data(data.links).join("line").attr("class", "link");

const color = d3.scaleOrdinal(d3.schemeTableau10);

const tooltip = d3.select("body").append("div")
  .attr("class", "tooltip").style("opacity", 0);

const node = g.append("g").selectAll("circle")
  .data(data.nodes).join("circle")
  .attr("class", "node").attr("r", 9)
  .attr("fill", d => color(d.source || "default"))
  .call(d3.drag()
    .on("start", (e, d) => {{ if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }})
    .on("drag", (e, d) => {{ d.fx = e.x; d.fy = e.y; }})
    .on("end", (e, d) => {{ if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }}))
  .on("mouseover", (e, d) => {{
    tooltip.transition().duration(150).style("opacity", 0.95);
    const conf = d.confidence != null ? (d.confidence * 100).toFixed(1) + "%" : "N/A";
    tooltip.html(`<strong>Fonte:</strong> ${{d.source || "—"}}<br>`
      + `<strong>Claim:</strong> ${{d.label || "—"}}<br>`
      + `<strong>Confiança:</strong> ${{conf}}`)
      .style("left", (e.pageX + 12) + "px").style("top", (e.pageY - 30) + "px");
  }})
  .on("mouseout", () => tooltip.transition().duration(400).style("opacity", 0));

const label = g.append("g").selectAll("text")
  .data(data.nodes).join("text")
  .attr("class", "label")
  .text(d => (d.label || "").substring(0, 22) + ((d.label || "").length > 22 ? "…" : ""));

sim.on("tick", () => {{
  link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
      .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  node.attr("cx", d => d.x).attr("cy", d => d.y);
  label.attr("x", d => d.x + 11).attr("y", d => d.y + 4);
}});
</script>
</body>
</html>
"""
            _components.html(_html, height=500, scrolling=False)
        except Exception as e:
            st.error(f"Erro ao renderizar visualização D3: {e}")

    # Painel de estatísticas do grafo
    if _eg:
        confirms = len(
            [
                r
                for r in getattr(_eg, "relations", [])
                if getattr(r, "relation_type", None) == "CONFIRMS"
            ]
        )
        contradicts = len(
            [
                r
                for r in getattr(_eg, "relations", [])
                if getattr(r, "relation_type", None) == "CONTRADICTS"
            ]
        )
        claims_count = len(getattr(_eg, "claims", []))

        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(
                f"""
                <div class="graph-stats">
                    <div class="stat-item">
                        <span class="stat-value">{claims_count}</span>
                        <span class="stat-label">Claims</span>
                    </div>
                </div>
            """,
                unsafe_allow_html=True,
            )
        with col2:
            st.markdown(
                f"""
                <div class="graph-stats">
                    <div class="stat-item">
                        <span class="stat-value">{confirms}</span>
                        <span class="stat-label">Confirmações</span>
                    </div>
                </div>
            """,
                unsafe_allow_html=True,
            )
        with col3:
            st.markdown(
                f"""
                <div class="graph-stats">
                    <div class="stat-item">
                        <span class="stat-value">{contradicts}</span>
                        <span class="stat-label">Contradições</span>
                    </div>
                </div>
            """,
                unsafe_allow_html=True,
            )

    if not _eg:
        st.info(
            "📊 Nenhum grafo de evidências disponível. Execute uma pesquisa para gerar as relações semânticas.\n\n"
            "O EvidenceGraph conectará claims de fontes diferentes (confirmando ou contradizendo)."
        )
