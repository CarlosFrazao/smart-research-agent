"""Interface Web Interativa do Smart Research Agent (Streamlit).

Refatorada para o SRA Upgrade v3.0 com estética premium,
tabs para o Lab Mode, Monitor do Swarm de Agentes e aprovações HITL.
"""

from __future__ import annotations

import streamlit as st
import asyncio
import json
from datetime import datetime

# Configuração da página Streamlit com estética premium
st.set_page_config(
    page_title="Smart Research Agent Studio v3.0",
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

        /* Badges de Confiança */
        .confidence-badge {
            background-color: #ebf8ff;
            color: #2b6cb0;
            font-weight: 600;
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# Renderização do cabeçalho
st.markdown(
    """
    <div class="top-bar">
        <h1>🛡️ Smart Research Agent Studio <span style="font-size:1.2rem; vertical-align:middle; background:#00c6ff; padding:2px 8px; border-radius:4px;">v3.0</span></h1>
        <p>Plataforma de pesquisa corporativa com multi-agentes, sandboxing e GraphRAG</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# Sidebar de Configurações
with st.sidebar:
    st.markdown("### ⚙️ Painel de Controle")
    mode = st.selectbox(
        "Modo de Operação",
        ["cirurgia", "guerrilha", "radar", "arqueologia", "concorrencia", "black_ops"],
        help="guerrilha=rápido, cirurgia=preciso com auditoria, black_ops=pesquisa profunda",
    )
    max_results = st.slider("Resultados máximos por fonte", 3, 25, 12)
    languages = st.multiselect(
        "Idiomas de Busca", ["en", "pt", "es", "zh"], default=["en", "pt"]
    )
    st.divider()
    st.markdown("### 🧬 Recursos Ativos")
    st.checkbox("Habilitar Docker Sandbox", value=True)
    st.checkbox("Habilitar GraphRAG (KuzuDB)", value=True)
    st.checkbox("Habilitar Análise de Contradições", value=True)
    st.divider()
    st.info("SRA v3.0 com resiliência total e economia de tokens integrada.")

# Abas de navegação principal (Lab Mode)
tab_search, tab_swarm, tab_gate, tab_graph = st.tabs(
    [
        "🔎 Console de Pesquisa",
        "🤖 Monitor do Swarm",
        "🚦 Gatekeeping (HITL)",
        "📊 Grafo de Conhecimento",
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
                await asyncio.sleep(1)

                status_text.info(
                    "🔍 [Query Expander] Expandindo sub-queries complementares..."
                )
                status_bar.progress(50)
                await asyncio.sleep(1)

                status_text.info(
                    "🌐 Executando buscas paralelas e ranqueando fontes..."
                )
                status_bar.progress(75)

                # Roda a orquestração assíncrona
                result = loop.run_until_complete(orch.research(query))

                status_bar.progress(100)
                status_text.success("Orquestração concluída!")

                st.markdown("---")
                st.markdown("### 📋 Relatório de Síntese")
                st.markdown(
                    result if isinstance(result, str) else json.dumps(result, indent=2)
                )

            except Exception as e:
                st.error(f"Erro no pipeline: {e}")

# ---- ABA 2: MONITOR DO SWARM ----
with tab_swarm:
    st.markdown("### 🤖 Monitoramento do Swarm de Agentes")
    st.write(
        "Acompanhe o estado de execução em tempo real de cada agente especializado:"
    )

    # Grid de Agentes com CSS Customizado
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown(
            """
            <div class="agent-card">
                <div class="agent-name">🧠 Intent Analyzer Agent</div>
                <div class="agent-status status-active">ATIVO</div>
                <p style="margin-top:0.5rem; color:#4a5568; font-size:0.9rem;">
                    Identifica domínios, conceitos fundamentais e a urgência do problema de busca do usuário.
                </p>
            </div>
            <div class="agent-card">
                <div class="agent-name">🔎 Query Expander Agent</div>
                <div class="agent-status status-idle">AGUARDANDO</div>
                <p style="margin-top:0.5rem; color:#4a5568; font-size:0.9rem;">
                    Bifurca o tópico em sub-queries sob diferentes perspectivas (STORM style) para máxima cobertura.
                </p>
            </div>
            <div class="agent-card">
                <div class="agent-name">⚖️ Quality Ranker Agent</div>
                <div class="agent-status status-idle">AGUARDANDO</div>
                <p style="margin-top:0.5rem; color:#4a5568; font-size:0.9rem;">
                    Ordena e descarta fontes baseado em autoridade, frescor e relevância de domínio.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col_b:
        st.markdown(
            """
            <div class="agent-card">
                <div class="agent-name">🛡️ Research Auditor Agent</div>
                <div class="agent-status status-idle">AGUARDANDO</div>
                <p style="margin-top:0.5rem; color:#4a5568; font-size:0.9rem;">
                    Verifica alegações "claim-by-claim", identificando gaps e emitindo contra-argumentações.
                </p>
            </div>
            <div class="agent-card">
                <div class="agent-name">📦 Docker Sandbox Execution Agent</div>
                <div class="agent-status status-idle">AGUARDANDO</div>
                <p style="margin-top:0.5rem; color:#4a5568; font-size:0.9rem;">
                    Roda trechos de código em containers descartáveis e isolados para comprovação lógica segura.
                </p>
            </div>
            <div class="agent-card">
                <div class="agent-name">🖋️ Synthesis & Report Agent</div>
                <div class="agent-status status-idle">AGUARDANDO</div>
                <p style="margin-top:0.5rem; color:#4a5568; font-size:0.9rem;">
                    Consolida os achados e formata citações bibliográficas de acordo com o domínio do problema.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

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

# ---- ABA 4: GRAFO DE CONHECIMENTO ----
with tab_graph:
    st.markdown("### 📊 Visualização do Grafo de Conhecimento (GraphRAG)")
    st.write("Entidades e relações identificadas no corpus de conhecimento coletado:")

    # Tabela Simulada de Relacionamentos do Grafo
    relationships = [
        {
            "Origem": "Python 3.12",
            "Relação": "INTRODUCES",
            "Destino": "PEP 709",
            "Confiança": "95%",
        },
        {
            "Origem": "PEP 709",
            "Relação": "OPTIMIZES",
            "Destino": "Inline Comprehensions",
            "Confiança": "98%",
        },
        {
            "Origem": "Inline Comprehensions",
            "Relação": "REDUCES",
            "Destino": "Interpreter Overhead",
            "Confiança": "85%",
        },
        {
            "Origem": "Python 3.12",
            "Relação": "IMPROVES",
            "Destino": "Garbage Collector Speed",
            "Confiança": "90%",
        },
    ]
    st.table(relationships)

    st.markdown("### 🔍 Gaps de Conhecimento Detectados no Grafo")
    st.warning(
        "O algoritmo de travessia do Grafo identificou que a relação **'PEP 709 -> WSL2 Memory Impact'** possui baixa densidade de evidências nas fontes atuais. Sugere-se expandir buscas para este gap."
    )
