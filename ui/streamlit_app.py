"""Interface Web Interativa do Smart Research Agent (Streamlit)."""
import streamlit as st
import asyncio
import json

st.set_page_config(
    page_title="Smart Research Agent v6.0",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.title("🔍 Smart Research Agent v6.0")
st.markdown("*Plataforma Corporativa de Pesquisa Autônoma com IA*")

with st.sidebar:
    st.header("⚙️ Painel de Configurações")
    mode = st.selectbox(
        "Modo de Operação",
        ["cirurgia", "guerrilha", "radar", "arqueologia", "concorrencia", "black_ops"],
        help="guerrilha=rápido, cirurgia=preciso com auditoria, black_ops=pesquisa profunda"
    )
    max_results = st.slider("Resultados máximos por fonte", 3, 20, 10)
    languages = st.multiselect(
        "Idiomas de Busca",
        ["en", "pt", "es", "zh"],
        default=["en", "pt"]
    )
    st.divider()
    st.info("O SRA v6.0 conta com resiliência total a bloqueios e falhas de rede.")

query = st.text_input(
    "Insira sua solicitação de pesquisa:",
    placeholder="Ex: melhores práticas de desenvolvimento com Rust e WebAssembly em 2026",
)

col_run, col_clear = st.columns([4, 1])
with col_run:
    run_btn = st.button("🚀 Iniciar Pesquisa", type="primary", use_container_width=True)
with col_clear:
    clear_btn = st.button("🗑️ Limpar Campos", use_container_width=True)

if run_btn and query:
    with st.spinner("Executando pipeline de pesquisa..."):
        status_bar = st.progress(0)
        status_text = st.empty()
        
        try:
            from src.config import Config
            from src.orchestrator import Orchestrator
            
            # Setup da configuração local com os inputs da tela
            config = Config()
            config.operation_mode = mode
            config.max_results_per_source = max_results
            
            orchestrator = Orchestrator(config)
            
            # Criação de loop de eventos isolado para thread segura do Streamlit
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            status_text.info("1/3 Analisando intenção e expandindo consultas...")
            status_bar.progress(33)
            
            # Execução assíncrona
            result = loop.run_until_complete(
                orchestrator.research(query)
            )
            
            status_bar.progress(100)
            status_text.success("Pesquisa finalizada!")
            
            st.markdown("---")
            st.markdown("## 📋 Relatório de Síntese Gerado")
            st.markdown(result if isinstance(result, str) else json.dumps(result, indent=2))
            
        except Exception as e:
            st.error(f"Ocorreu um erro durante a pesquisa: {e}")