"""Interface Web Interativa do Smart Research Agent (Streamlit).

Refatorada para o SRA Upgrade v6.2.0 com visualização melhorada do EvidenceGraph.
"""

from __future__ import annotations

import streamlit as st
import asyncio
import json
import time
from datetime import datetime

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
        ],
        help=(
            "guerrilha=rápido, cirurgia=preciso com auditoria, "
            "black_ops=pesquisa profunda, "
            "debate=motor multi-agente (hipóteses opostas + juiz LLM)"
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

            try:
                from src.config import Config
                from src.orchestrator import Orchestrator

                config = Config()
                config.operation_mode = mode
                config.max_results_per_source = max_results

                orch = Orchestrator(config)
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                status_text.info(
                    "🧬 [Intent Analyzer] Analisando intenção e extraindo conceitos..."
                )
                status_bar.progress(25)
                time.sleep(1)

                status_text.info(
                    "🔍 [Query Expander] Expandindo sub-queries complementares..."
                )
                status_bar.progress(50)
                time.sleep(1)

                status_text.info(
                    "🌐 Executando buscas paralelas e ranqueando fontes..."
                )
                status_bar.progress(75)

                # Roda a orquestração assíncrona
                result = loop.run_until_complete(orch.research(query))

                # Salva a instância do orquestrador para uso em outras abas (ex: Grafo)
                st.session_state["orch"] = orch

                status_bar.progress(100)
                status_text.success("Orquestração concluída!")

                st.markdown("---")
                st.markdown("### 📋 Relatório de Síntese")
                st.markdown(
                    result if isinstance(result, str) else json.dumps(result, indent=2)
                )

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
