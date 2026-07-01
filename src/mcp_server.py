import asyncio
import glob as glob_module
import json
import logging
import os
from typing import Optional

from fastapi import FastAPI
from fastapi import Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.confidence_scorer import ConfidenceScorer
from src.deep_researcher import DeepResearcher
from src.feedback_store import FeedbackStore, VALID_SIGNALS
from src.orchestrator import Orchestrator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

app = FastAPI(title="Smart Research Agent MCP Server")
_orchestrator: Optional[Orchestrator] = None
_deep_researcher: Optional[DeepResearcher] = None
_confidence_scorer: Optional[ConfidenceScorer] = None

_STATIC_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "static"))
_REPORTS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "reports"))

if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


def get_deep_researcher() -> DeepResearcher:
    global _deep_researcher
    if _deep_researcher is None:
        orch = get_orchestrator()
        _deep_researcher = DeepResearcher(llm_client=orch.llm, orchestrator=orch, memory=orch.memory)
    return _deep_researcher


def get_confidence_scorer() -> ConfidenceScorer:
    global _confidence_scorer
    if _confidence_scorer is None:
        _confidence_scorer = ConfidenceScorer()
    return _confidence_scorer


@app.get("/health")
async def health():
    return {"status": "ok", "service": "smart-research-agent"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    favicon_path = os.path.join(_STATIC_DIR, "favicon.ico")
    if os.path.isfile(favicon_path):
        return FileResponse(favicon_path)
    return Response(status_code=204)


@app.get("/", response_class=FileResponse)
async def serve_dashboard():
    """Serve o dashboard SPA principal."""
    index_path = os.path.join(_STATIC_DIR, "index.html")
    if not os.path.isfile(index_path):
        return PlainTextResponse("Dashboard não encontrado. Crie static/index.html.", status_code=404)
    return FileResponse(index_path)


@app.get("/api/reports")
async def list_reports():
    """Lista todos os relatórios Markdown gerados na pasta reports/."""
    if not os.path.isdir(_REPORTS_DIR):
        return {"reports": []}
    files = sorted(
        glob_module.glob(os.path.join(_REPORTS_DIR, "*.md")),
        reverse=True,
    )
    result = []
    for f in files:
        if os.path.basename(f).startswith("_"):
            continue
        stat = os.stat(f)
        result.append({
            "filename": os.path.basename(f),
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
        })
    return {"reports": result}


@app.get("/api/reports/{filename}")
async def get_report(filename: str):
    """Retorna o conteúdo de um relatório Markdown específico."""
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".md") or safe_name.startswith("_"):
        return PlainTextResponse("Arquivo inválido.", status_code=400)
    file_path = os.path.join(_REPORTS_DIR, safe_name)
    if not os.path.isfile(file_path):
        return PlainTextResponse("Relatório não encontrado.", status_code=404)
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    return PlainTextResponse(content, media_type="text/markdown")


@app.post("/api/chat")
async def chat_direct(body: dict):
    """
    Chat direto com LLM sem pipeline de pesquisa.
    Body: { model, messages, system_prompt, api_key?, provider? }
    api_key e provider opcionais — sobrepõem as chaves do .env sem as logar.
    Retorna streaming SSE com chunks de texto.
    """
    from src.clients.llm_client import LLMClient
    from src.clients.llm_client import LLMProvider as ClientLLMProvider
    from src.config import Config

    messages = body.get("messages", [])
    system_prompt = body.get("system_prompt", "Você é um assistente de pesquisa especializado e útil.")
    user_api_key = body.get("api_key") or None
    user_provider = body.get("provider") or None

    if not messages:
        return {"error": "messages é obrigatório"}

    config = Config()
    llm_config = config.get_llm_config()

    # Override silencioso — chave nunca é logada
    if user_api_key:
        llm_config["api_key"] = user_api_key

    try:
        provider_type = ClientLLMProvider(user_provider) if user_provider else ClientLLMProvider(config.llm_provider)
    except ValueError:
        provider_type = ClientLLMProvider(config.llm_provider)

    async def generate():
        try:
            llm = LLMClient(provider_type, llm_config)
            full_prompt = f"{system_prompt}\n\n"
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                full_prompt += f"{role.upper()}: {content}\n"
            full_prompt += "ASSISTANT:"

            response = await llm.complete(full_prompt, max_tokens=2048)
            chunk_size = 50
            for i in range(0, len(response), chunk_size):
                chunk = response[i : i + chunk_size]
                yield f"data: {chunk}\n\n"
                await asyncio.sleep(0.01)
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"[api/chat] erro: {e}")
            yield f"data: [ERROR] {e}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/feedback")
async def feedback_endpoint(body: dict):
    """
    Registra feedback via REST (usado pelo dashboard).
    Body: { query: str, signal: str }  — signal: helpful | not_helpful
    """
    query = body.get("query", "")
    signal_raw = body.get("signal", "")

    signal_map = {"helpful": "useful", "not_helpful": "not_useful"}
    signal = signal_map.get(signal_raw, signal_raw)

    try:
        store = FeedbackStore()
        import hashlib
        result_id = hashlib.sha1(query.lower().encode()).hexdigest()[:12]
        entry = store.record(result_id=result_id, signal=signal, query=query)
        return {"recorded": True, "entry": entry}
    except ValueError as e:
        return {"recorded": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[feedback] erro: {e}")
        return {"recorded": False, "error": str(e)}


@app.post("/api/obsidian-sync")
async def obsidian_sync_endpoint(body: dict):
    """
    Copia o último relatório gerado para o Obsidian Vault configurado em OBSIDIAN_VAULT_PATH.
    Body: { filename: str } — query ou nome do arquivo
    """
    from src.config import Config
    import shutil

    config = Config()
    vault_path = getattr(config, "obsidian_vault_path", None)
    if not vault_path:
        return PlainTextResponse("OBSIDIAN_VAULT_PATH não configurado no .env", status_code=400)

    query_or_file = body.get("filename", "")
    if not query_or_file:
        return PlainTextResponse("filename é obrigatório", status_code=400)

    if not os.path.isdir(_REPORTS_DIR):
        return PlainTextResponse("Pasta reports/ não encontrada.", status_code=404)

    candidates = sorted(
        glob_module.glob(os.path.join(_REPORTS_DIR, "*.md")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not candidates:
        return PlainTextResponse("Nenhum relatório disponível.", status_code=404)

    src_file = candidates[0]
    try:
        os.makedirs(vault_path, exist_ok=True)
        dest = os.path.join(vault_path, os.path.basename(src_file))
        shutil.copy2(src_file, dest)
        logger.info(f"Obsidian sync: {src_file} → {dest}")
        return {"synced": True, "destination": dest}
    except Exception as e:
        logger.error(f"[obsidian-sync] erro: {e}")
        return {"synced": False, "error": str(e)}


@app.post("/research")
async def research_endpoint(body: dict):
    query = body.get("query", "")
    if not query:
        return {"error": "query is required"}
    # api_key e provider são aceitos no body para compatibilidade com o frontend
    # O orchestrator usa as chaves do .env por default; override não é logado
    try:
        report = await get_orchestrator().research(query)
        return {"report": report, "query": query}
    except Exception as e:
        logger.error(f"Erro na pesquisa: {e}")
        return {"error": str(e)}


try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("Smart Research Agent")

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 1 — Pesquisa profunda completa (pipeline de 9 passos)
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def research_technology(query: str, op_mode: str = None) -> str:
        """
        Executa uma pesquisa profunda e completa sobre tecnologia, SaaS, automacao
        ou desenvolvimento open source, percorrendo 9 passos internos.

        Suporta presets de operacao via op_mode (guerrilha, cirurgia, radar, arqueologia,
        concorrencia, black_ops). Se omitido, auto-seleciona com base na query.

        Args:
            query: A query de pesquisa em linguagem natural.
            op_mode: Opcional preset de operacao (guerrilha, cirurgia, radar, arqueologia, concorrencia, black_ops).
        """
        try:
            logger.info(f"[research_technology] query='{query}' op_mode={op_mode}")
            orc = get_orchestrator()
            from src.operation_modes import OperationModes
            selected_op = op_mode or OperationModes.auto_select(query)
            orc.operation_mode = OperationModes.get_mode(selected_op)
            return await orc.research(query)
        except Exception as e:
            logger.error(f"[research_technology] erro: {e}")
            return f"Erro ao executar pesquisa profunda: {e}"

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 2 — Busca no GitHub
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def search_github(query: str, domain: str = "general", max_results: int = 10) -> str:
        """
        Busca repositorios, projetos e codigo diretamente no GitHub.
        Retorna lista JSON com titulo, URL, descricao, stars, forks e linguagem.

        Ideal para: encontrar bibliotecas open source, comparar projetos por popularidade
        (stars/forks), descobrir projetos ativos de um ecosistema especifico.

        Args:
            query: Termos de busca (ex: "self-hosted CRM python", "n8n alternative")
            domain: Dominio para contexto — um de: saas_b2b, dev_tools, ai_ml,
                    automation, infrastructure, open_source, general (padrao: general)
            max_results: Numero maximo de resultados (padrao: 10, max: 30)
        """
        try:
            orc = get_orchestrator()
            searcher = orc.searchers.get("github")
            if not searcher:
                return json.dumps({"error": "GitHub searcher nao disponivel"})
            searcher.max_results = min(max_results, 30)
            results = await searcher.search(query, domain=domain)
            data = [
                {
                    "title": r.title,
                    "url": r.url,
                    "description": r.description,
                    "source": r.source,
                    "metrics": r.metrics,
                }
                for r in results
            ]
            logger.info(f"[search_github] {len(data)} resultados para '{query}'")
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[search_github] erro: {e}")
            return json.dumps({"error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 3 — Busca no Reddit
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def search_reddit(query: str, domain: str = "general", max_results: int = 10) -> str:
        """
        Busca discussoes, recomendacoes e opinioes reais de usuarios no Reddit.
        Retorna lista JSON com titulo, URL, subreddit, descricao e upvotes.

        Ideal para: opinioes organicas sobre ferramentas, relatos de experiencia real,
        comparativos feitos pela comunidade, threads de recomendacao.

        Args:
            query: Termos de busca (ex: "best open source CRM reddit", "n8n vs make")
            domain: Dominio para contexto (saas_b2b, dev_tools, ai_ml, automation,
                    infrastructure, open_source, general). Padrao: general
            max_results: Numero maximo de resultados (padrao: 10, max: 30)
        """
        try:
            orc = get_orchestrator()
            searcher = orc.searchers.get("reddit")
            if not searcher:
                return json.dumps({"error": "Reddit searcher nao disponivel"})
            searcher.max_results = min(max_results, 30)
            results = await searcher.search(query, domain=domain)
            data = [
                {
                    "title": r.title,
                    "url": r.url,
                    "description": r.description,
                    "source": r.source,
                    "metrics": r.metrics,
                }
                for r in results
            ]
            logger.info(f"[search_reddit] {len(data)} resultados para '{query}'")
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[search_reddit] erro: {e}")
            return json.dumps({"error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 4 — Busca no Hacker News
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def search_hackernews(query: str, domain: str = "general", max_results: int = 10) -> str:
        """
        Busca stories, Ask HN e discussoes tecnicas no Hacker News (YCombinator).
        Retorna lista JSON com titulo, URL, descricao, pontuacao e comentarios.

        Ideal para: tendencias tecnicas, debates sobre ferramentas emergentes,
        opinioes de engenheiros seniores, launches de produtos tech.

        Args:
            query: Termos de busca (ex: "self-hosted analytics", "LLM production")
            domain: Dominio para contexto (padrao: general)
            max_results: Numero maximo de resultados (padrao: 10, max: 30)
        """
        try:
            orc = get_orchestrator()
            searcher = orc.searchers.get("hackernews")
            if not searcher:
                return json.dumps({"error": "HackerNews searcher nao disponivel"})
            searcher.max_results = min(max_results, 30)
            results = await searcher.search(query, domain=domain)
            data = [
                {
                    "title": r.title,
                    "url": r.url,
                    "description": r.description,
                    "source": r.source,
                    "metrics": r.metrics,
                }
                for r in results
            ]
            logger.info(f"[search_hackernews] {len(data)} resultados para '{query}'")
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[search_hackernews] erro: {e}")
            return json.dumps({"error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 5 — Busca em Awesome Lists
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def search_awesome_lists(query: str, domain: str = "general", max_results: int = 15) -> str:
        """
        Busca ferramentas e recursos curados em Awesome Lists do GitHub.
        Retorna lista JSON com titulo, URL e descricao dos itens encontrados.

        Ideal para: descobrir as ferramentas mais reconhecidas de um ecosistema,
        listas curadas pela comunidade, catalogo de opcoes por categoria.

        Args:
            query: Termos de busca (ex: "self-hosted", "python web framework", "LLM tools")
            domain: Dominio para contexto (padrao: general)
            max_results: Numero maximo de resultados (padrao: 15, max: 50)
        """
        try:
            orc = get_orchestrator()
            searcher = orc.searchers.get("awesome")
            if not searcher:
                return json.dumps({"error": "Awesome Lists searcher nao disponivel"})
            searcher.max_results = min(max_results, 50)
            results = await searcher.search(query, domain=domain)
            data = [
                {
                    "title": r.title,
                    "url": r.url,
                    "description": r.description,
                    "source": r.source,
                    "metrics": r.metrics,
                }
                for r in results
            ]
            logger.info(f"[search_awesome_lists] {len(data)} resultados para '{query}'")
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[search_awesome_lists] erro: {e}")
            return json.dumps({"error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 6 — Busca no ArXiv (papers academicos)
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def search_arxiv(query: str, domain: str = "ai_ml", max_results: int = 10) -> str:
        """
        Busca artigos e papers academicos no ArXiv (pre-prints de ciencia da computacao,
        IA, ML, matematica e areas correlatas). Retorna lista JSON com titulo, URL,
        autores, resumo e data de publicacao.

        Ideal para: embasamento academico sobre tecnicas de IA/ML, encontrar
        papers sobre algoritmos, arquiteturas de modelos e pesquisas recentes.

        Args:
            query: Termos de busca (ex: "RAG retrieval augmented generation",
                   "transformer architecture optimization")
            domain: Dominio para contexto (padrao: ai_ml)
            max_results: Numero maximo de resultados (padrao: 10, max: 20)
        """
        try:
            orc = get_orchestrator()
            searcher = orc.searchers.get("arxiv")
            if not searcher:
                return json.dumps({"error": "ArXiv searcher nao disponivel"})
            searcher.max_results = min(max_results, 20)
            results = await searcher.search(query, domain=domain)
            data = [
                {
                    "title": r.title,
                    "url": r.url,
                    "description": r.description,
                    "source": r.source,
                    "metrics": r.metrics,
                }
                for r in results
            ]
            logger.info(f"[search_arxiv] {len(data)} resultados para '{query}'")
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[search_arxiv] erro: {e}")
            return json.dumps({"error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 7 — Busca no Product Hunt
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def search_producthunt(query: str, domain: str = "saas_b2b", max_results: int = 10) -> str:
        """
        Busca produtos e launches no Product Hunt. Retorna lista JSON com titulo,
        URL, descricao, tagline, votos e data de lancamento.

        Ideal para: descobrir SaaS recentes, produtos inovadores, alternativas a
        ferramentas conhecidas, tendencias de mercado de produtos tech.

        Args:
            query: Termos de busca (ex: "CRM startup", "AI writing tool", "automation")
            domain: Dominio para contexto (padrao: saas_b2b)
            max_results: Numero maximo de resultados (padrao: 10, max: 20)
        """
        try:
            orc = get_orchestrator()
            searcher = orc.searchers.get("producthunt")
            if not searcher:
                return json.dumps({"error": "ProductHunt searcher nao disponivel"})
            searcher.max_results = min(max_results, 20)
            results = await searcher.search(query, domain=domain)
            data = [
                {
                    "title": r.title,
                    "url": r.url,
                    "description": r.description,
                    "source": r.source,
                    "metrics": r.metrics,
                }
                for r in results
            ]
            logger.info(f"[search_producthunt] {len(data)} resultados para '{query}'")
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[search_producthunt] erro: {e}")
            return json.dumps({"error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 8 — Busca web geral
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def search_web(query: str, domain: str = "general", max_results: int = 10) -> str:
        """
        Realiza busca web geral usando o WebSearcher interno (DuckDuckGo/SerpAPI).
        Retorna lista JSON com titulo, URL e snippet de cada resultado.

        Ideal para: buscar informacoes gerais, documentacao de produtos, artigos
        de blogs, tutoriais e qualquer conteudo publico na web.

        Args:
            query: Termos de busca (ex: "como configurar n8n self-hosted Docker")
            domain: Dominio para contexto (padrao: general)
            max_results: Numero maximo de resultados (padrao: 10, max: 20)
        """
        try:
            orc = get_orchestrator()
            searcher = orc.searchers.get("web")
            if not searcher:
                return json.dumps({"error": "Web searcher nao disponivel"})
            searcher.max_results = min(max_results, 20)
            results = await searcher.search(query, domain=domain)
            data = [
                {
                    "title": r.title,
                    "url": r.url,
                    "description": r.description,
                    "source": r.source,
                    "metrics": r.metrics,
                }
                for r in results
            ]
            logger.info(f"[search_web] {len(data)} resultados para '{query}'")
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[search_web] erro: {e}")
            return json.dumps({"error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 9 — Scraping via Firecrawl (extrai conteudo de URL especifica)
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def scrape_with_firecrawl(query: str, domain: str = "general", max_results: int = 5) -> str:
        """
        Usa o Firecrawl (instancia local Docker na porta 3002) para extrair
        conteudo de paginas web, incluindo sites com JavaScript, SPAs e paginas
        protegidas contra bots. Retorna o conteudo extraido em Markdown.

        Use quando: a busca web retornar links mas precisar do conteudo completo
        de uma pagina; quando o site exigir renderizacao JS; como complemento ao
        search_web para extrair detalhes de URLs especificas encontradas.

        IMPORTANTE: Esta tool usa sua instancia LOCAL do Firecrawl (Docker).
        O Firecrawl deve estar rodando (porta 3002) para funcionar.

        Args:
            query: URL ou termo de busca para o Firecrawl processar
            domain: Dominio para contexto (padrao: general)
            max_results: Numero maximo de resultados (padrao: 5, max: 10)
        """
        try:
            orc = get_orchestrator()
            searcher = orc.searchers.get("firecrawl")
            if not searcher:
                return json.dumps({"error": "Firecrawl searcher nao disponivel"})
            searcher.max_results = min(max_results, 10)
            results = await searcher.search(query, domain=domain)
            data = [
                {
                    "title": r.title,
                    "url": r.url,
                    "description": r.description,
                    "source": r.source,
                    "metrics": r.metrics,
                }
                for r in results
            ]
            logger.info(f"[scrape_with_firecrawl] {len(data)} resultados para '{query}'")
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[scrape_with_firecrawl] erro: {e}")
            return json.dumps({"error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 10 — Analise de intencao de query
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def analyze_query_intent(query: str) -> str:
        """
        Analisa uma query de pesquisa e retorna seu dominio, intencao, entidades
        detectadas, urgencia e nivel de confianca da classificacao.

        Use antes de uma pesquisa para entender melhor a natureza da query e
        direcionar o uso das tools corretas. Util como step de planeamento.

        Retorna JSON com:
        - domain: saas_b2b | dev_tools | ai_ml | automation | infrastructure
                  | open_source | general
        - intention: discover | compare | learn | implement | evaluate
        - entities: lista de produtos/empresas/tecnologias detectados
        - urgency: sim | nao (se a query menciona novidades recentes)
        - confidence: alta | media | baixa

        Args:
            query: A query a ser analisada (ex: "compare n8n vs Zapier 2026")
        """
        try:
            orc = get_orchestrator()
            intent = await orc.intent_analyzer.analyze(query)
            result = {
                "domain": intent.domain.value,
                "intention": intent.intention.value,
                "entities": intent.entities,
                "urgency": intent.urgency,
                "confidence": intent.confidence,
            }
            logger.info(f"[analyze_query_intent] domain={intent.domain.value} intention={intent.intention.value}")
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[analyze_query_intent] erro: {e}")
            return json.dumps({"error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 11 — Expansao de queries
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def expand_query(query: str) -> str:
        """
        Expande uma query de pesquisa em multiplas variacoes otimizadas para
        diferentes fontes e angulos de busca. Usa LLM para gerar sinonimos,
        qualificadores, comparacoes e casos de uso relacionados.

        Retorna JSON com lista de queries expandidas, cada uma contendo:
        - query: o texto da query expandida
        - type: sinonimo | qualificador | plataforma | comparacao | caso_de_uso | gap_fill
        - priority: alta | media | baixa
        - rationale: justificativa para a expansao

        Use quando quiser realizar buscas manuais mais abrangentes nas tools
        individuais (search_github, search_reddit, etc.) apos expandir a query.

        Args:
            query: Query original a ser expandida (ex: "CRM open source")
        """
        try:
            orc = get_orchestrator()
            intent = await orc.intent_analyzer.analyze(query)
            expanded = await orc.query_expander.expand(query, intent)
            data = [
                {
                    "query": eq.query,
                    "type": eq.type,
                    "priority": eq.priority,
                    "rationale": eq.rationale,
                }
                for eq in expanded
            ]
            logger.info(f"[expand_query] {len(data)} queries expandidas para '{query}'")
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[expand_query] erro: {e}")
            return json.dumps({"error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 12 — Research v2 com suporte a modo deep e scores de confiança
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def research_technology_v2(
        query: str,
        mode: str = "standard",
        include_confidence: bool = True,
        op_mode: str = None,
    ) -> str:
        """
        Versão aprimorada do research_technology com suporte a raciocínio profundo,
        scores de confiança anti-alucinação e presets de operação (op_mode).

        Modos de raciocínio (mode):
        - "standard": pipeline de 9 passos (mais rápido)
        - "deep": raciocínio em árvore com hipóteses concorrentes (~5x mais lento e custoso)

        Modos de operação (op_mode):
        - "guerrilha", "cirurgia", "radar", "arqueologia", "concorrencia", "black_ops"
        """
        try:
            logger.info(f"[research_technology_v2] query='{query}' mode={mode} op_mode={op_mode}")
            orc = get_orchestrator()
            from src.operation_modes import OperationModes
            selected_op = op_mode or OperationModes.auto_select(query)
            orc.operation_mode = OperationModes.get_mode(selected_op)

            if mode == "deep":
                result = await get_deep_researcher().research(query)
                confirmed_count = len(result.confirmed_hypotheses)
                dead_end_count = len(result.dead_end_hypotheses)
                overall_confidence = (
                    sum(getattr(f, "confidence_score", 0.0) for f in result.findings)
                    / len(result.findings)
                    if result.findings
                    else 0.0
                )
                findings_lines = []
                for i, f in enumerate(result.findings[:15], 1):
                    title = f.title or "(sem título)"
                    url = f.url or ""
                    desc = (f.description or "")[:200]
                    conf = getattr(f, "confidence_score", 0.0)
                    findings_lines.append(
                        f"### {i}. {title}\n- URL: {url}\n- Confiança: {conf:.0%}\n- {desc}"
                    )
                report = "\n\n".join(findings_lines) if findings_lines else "(nenhum resultado encontrado)"
                if include_confidence:
                    tree_md = result.reasoning_tree or ""
                    confidence_lines = [
                        "",
                        "---",
                        "",
                        "## Confidence Summary",
                        "",
                        f"- Overall confidence: {overall_confidence:.0%}",
                        f"- High-confidence findings: {confirmed_count}",
                        f"- Dead-end branches pruned: {dead_end_count}",
                        "",
                        tree_md,
                    ]
                    report = report + "\n".join(str(line) for line in confidence_lines)
                return report
            else:
                return await orc.research(query)
        except Exception as e:
            logger.error(f"[research_technology_v2] erro: {e}")
            return f"Erro ao executar pesquisa: {e}"

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 13 — Scraping com cascade inteligente (Firecrawl→Spider→Steel→Jina)
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def scrape_url(url: str, force_browser: bool = False) -> str:
        """
        Extrai o conteúdo de uma URL usando cascade inteligente de scrapers.

        Ordem de tentativa (automática):
        1. Firecrawl (padrão — markdown limpo, JS básico)
        2. Spider.cloud (se Firecrawl falhar, ultra-rápido para crawling)
        3. Steel.dev (se Spider falhar, browser completo para JS pesado)
        4. Jina Reader (fallback final zero-config: r.jina.ai/{url})

        force_browser=True pula direto para Steel.dev, ideal para SPAs,
        páginas com login ou conteúdo gerado por JavaScript intensivo.

        Retorna JSON com:
        - url: a URL original
        - content: conteúdo extraído em Markdown
        - scraper_used: qual scraper teve sucesso
        - confidence_score: score de confiança do conteúdo extraído

        Args:
            url: URL completa para extrair (ex: "https://github.com/org/repo")
            force_browser: Se True, força uso do Steel.dev (padrão: False)
        """
        try:
            logger.info(f"[scrape_url] url='{url}' force_browser={force_browser}")
            orc = get_orchestrator()
            results = await orc._select_scraper_for_url(url)
            if not results:
                return json.dumps({"url": url, "content": "", "scraper_used": "none", "error": "Nenhum conteúdo extraído"})
            r = results[0]
            scored = await get_confidence_scorer().score_result(r)
            return json.dumps(
                {
                    "url": url,
                    "content": scored.description,
                    "title": scored.title,
                    "scraper_used": scored.source,
                    "confidence_score": round(scored.confidence_score, 3),
                    "evidence_quality": scored.evidence_quality,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            logger.error(f"[scrape_url] erro: {e}")
            return json.dumps({"url": url, "error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 14 — Verificação de confiança de uma afirmação contra fontes reais
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def confidence_check(claim: str, sources: list[str]) -> str:
        """
        Verifica a confiança de uma afirmação contra uma lista de URLs de fontes.

        Para cada URL fornecida, extrai o conteúdo e aplica o ConfidenceScorer
        para calcular se a afirmação é suportada, contradita ou sem evidência.

        Retorna JSON com:
        - claim: a afirmação original
        - overall_confidence: score médio ponderado (0.0-1.0)
        - evidence_quality: "verified" | "cited" | "inferred" | "unknown"
        - supporting_sources: URLs que suportam a afirmação
        - contradicting_sources: URLs que contradizem
        - hallucination_flags: alertas detectados
        - recommendation: "use_with_confidence" | "verify_further" | "do_not_use"

        Args:
            claim: A afirmação a verificar (ex: "FastAPI é mais rápido que Flask")
            sources: Lista de URLs de fontes para checar (max 5)
        """
        try:
            logger.info(f"[confidence_check] claim='{claim[:80]}' sources={len(sources)}")
            scorer = get_confidence_scorer()
            orc = get_orchestrator()

            from src.types import SearchResult
            scored_results = []
            for url in sources[:5]:
                try:
                    raw = await orc._select_scraper_for_url(url)
                    if raw:
                        scored = await scorer.score_result(raw[0])
                        scored_results.append(scored)
                except Exception as src_err:
                    logger.warning(f"[confidence_check] falha ao processar {url}: {src_err}")

            # Fallback chain se scraping direto falhou para todas as fontes (BUG-09)
            if not scored_results:
                logger.warning(f"[confidence_check] Scraping falhou para todas as fontes. Iniciando fallback de busca para '{claim[:50]}'...")
                fallback_searchers = ["github", "hackernews", "web"]
                fallback_results = []
                for s_name in fallback_searchers:
                    searcher = orc.searchers.get(s_name)
                    if searcher and searcher.enabled:
                        try:
                            res = await searcher.search(claim[:100])
                            if res:
                                fallback_results.extend(res[:2])
                        except Exception as e:
                            logger.debug(f"[confidence_check] Fallback de busca em '{s_name}' falhou: {e}")

                for r in fallback_results:
                    try:
                        scored = await scorer.score_result(r)
                        scored_results.append(scored)
                    except Exception:
                        pass

            if not scored_results:
                return json.dumps({
                    "claim": claim,
                    "overall_confidence": 0.45,
                    "evidence_quality": "unverified",
                    "supporting_sources": [],
                    "contradicting_sources": [],
                    "hallucination_flags": ["scraper_unavailable"],
                    "recommendation": "verify_further",
                    "note": "Scrapers indisponiveis e busca de fallback nao retornou resultados. Verificacao manual recomendada."
                })

            scores = [r.confidence_score for r in scored_results]
            overall = sum(scores) / len(scores)

            supporting = [r.url for r in scored_results if r.confidence_score >= 0.55]
            contradicting = [r.url for r in scored_results if r.contradictions]
            all_flags: list[str] = []
            for r in scored_results:
                all_flags.extend(r.hallucination_flags)
            unique_flags = list(dict.fromkeys(all_flags))

            if overall >= 0.75:
                recommendation = "use_with_confidence"
            elif overall >= 0.45:
                recommendation = "verify_further"
            else:
                recommendation = "do_not_use"

            quality_levels = [r.evidence_quality for r in scored_results]
            best_quality = next(
                (q for q in ("verified", "cited", "inferred") if q in quality_levels),
                "unknown",
            )

            return json.dumps(
                {
                    "claim": claim,
                    "overall_confidence": round(overall, 3),
                    "evidence_quality": best_quality,
                    "supporting_sources": supporting,
                    "contradicting_sources": contradicting,
                    "hallucination_flags": unique_flags,
                    "recommendation": recommendation,
                    "sources_checked": len(scored_results),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            logger.error(f"[confidence_check] erro: {e}")
            return json.dumps({"claim": claim, "error": str(e)})

    # ─────────────────────────────────────────────────────────────────────────
    # TOOL 15 — Registrar feedback de resultado (FeedbackRanker)
    # ─────────────────────────────────────────────────────────────────────────
    @mcp.tool()
    async def record_feedback(result_id: str, signal: str, query: str = "") -> str:
        """
        Registra feedback sobre um resultado de pesquisa para melhorar o ranking futuro.

        O feedback é persistido em reports/_feedback.jsonl e aplicado automaticamente
        nas próximas sínteses pelo FeedbackRanker, que ajusta o combined_score em
        até ±15 pontos por resultado.

        Sinais válidos:
        - "useful"      — resultado foi útil e relevante (+1.5 pts)
        - "bookmark"    — marcar para referência futura (+2.0 pts)
        - "not_useful"  — resultado não ajudou na pesquisa (-1.0 pts)
        - "irrelevant"  — completamente fora do assunto (-1.5 pts)
        - "outdated"    — informação desatualizada (-0.5 pts)

        O result_id pode ser obtido via result_id_for(result) no FeedbackRanker,
        ou construído como sha1(f"{entity}:{title}".lower())[:12].

        Args:
            result_id: Identificador único do resultado (12 chars hex)
            signal: Sinal de feedback — um dos 5 sinais válidos acima
            query: Query original da pesquisa (opcional, para rastreabilidade)
        """
        try:
            store = FeedbackStore()
            entry = store.record(result_id=result_id, signal=signal, query=query)
            logger.info(f"[record_feedback] {result_id} → {signal}")
            return json.dumps(
                {
                    "recorded": True,
                    "result_id": entry["result_id"],
                    "signal": entry["signal"],
                    "timestamp": entry["timestamp"],
                    "valid_signals": sorted(VALID_SIGNALS),
                },
                ensure_ascii=False,
                indent=2,
            )
        except ValueError as e:
            return json.dumps({"recorded": False, "error": str(e), "valid_signals": sorted(VALID_SIGNALS)})
        except Exception as e:
            logger.error(f"[record_feedback] erro: {e}")
            return json.dumps({"recorded": False, "error": str(e)})

    app.mount("/mcp", mcp.sse_app())
    logger.info("MCP FastMCP montado com sucesso via sse_app() em /mcp — 15 tools registradas")

except ImportError as err:
    logger.warning(
        f"Erro ao carregar FastMCP: {err} — servidor SSE MCP indisponivel. Apenas endpoints REST ativos."
    )
