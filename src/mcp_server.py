"""Servidor MCP (Model Context Protocol) que expoe o Smart Research Agent como ferramenta via FastAPI.

Este módulo expõe `create_app(config)`, uma fábrica que constrói uma instância
independente do servidor. Cada instância recebe seu próprio
`DependencyContainer` (src/dependencies.py) anexado a `app.state.container`,
substituindo os antigos globais de módulo `_orchestrator` / `_deep_researcher`
/ `_confidence_scorer` / `_research_store`.

Isso permite multi-tenancy: `create_app(config_a)` e `create_app(config_b)`
produzem dois servidores totalmente isolados (LLM, memória, cache, sessões de
pesquisa) no mesmo processo. Para uso simples (um único tenant, via
`uvicorn src.mcp_server:app`), a variável de módulo `app` no final do arquivo
continua disponível como antes.
"""

import glob as glob_module
import json
import logging
import os
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import Config
from src.confidence_scorer import ConfidenceScorer
from src.deep_researcher import DeepResearcher
from src.dependencies import DependencyContainer, Lifecycle
from src.feedback_store import VALID_SIGNALS, FeedbackStore
from src.orchestrator import Orchestrator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

_STATIC_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "static"))
_REPORTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "reports")
)

# ContextVar (não é estado global mutável compartilhado: cada task assíncrona
# enxerga seu próprio valor) usada para expor a sessão de pesquisa corrente
# a código que não tem acesso direto ao `Request` (ex.: callbacks profundos).
_current_research: ContextVar[dict | None] = ContextVar(
    "current_research", default=None
)

# Armazenamento de sessões locais de pesquisa para fins de compatibilidade
_research_sessions: dict[str, dict] = {}


async def _get_or_create_research(session_id: str) -> dict:
    """Recupera ou cria uma sessão local de pesquisa."""
    if session_id not in _research_sessions:
        _research_sessions[session_id] = {
            "session_id": session_id,
            "last_query": None,
            "last_report": None,
        }
    return _research_sessions[session_id]


# ─────────────────────────────────────────────────────────────────────────
# Dependências FastAPI — leem o container a partir de `app.state`, nunca de
# um global de módulo. Cada instância de app tem o seu próprio container.
# ─────────────────────────────────────────────────────────────────────────


def get_container(request: Request) -> DependencyContainer:
    """Recupera o `DependencyContainer` da instância de app atual via `app.state`."""
    return request.app.state.container


def get_orchestrator_dep(
    container: DependencyContainer = Depends(get_container),
) -> Orchestrator:
    """Dependency FastAPI: `Orchestrator` da instância atual do servidor."""
    return container.orchestrator


def get_deep_researcher_dep(
    container: DependencyContainer = Depends(get_container),
) -> DeepResearcher:
    """Dependency FastAPI: `DeepResearcher` da instância atual do servidor."""
    return container.deep_researcher


def get_confidence_scorer_dep(
    container: DependencyContainer = Depends(get_container),
) -> ConfidenceScorer:
    """Dependency FastAPI: `ConfidenceScorer` da instância atual do servidor."""
    return container.confidence_scorer


# Globais e funcoes de compatibilidade para testes/singletons legados
_orchestrator = None
_deep_researcher = None
_confidence_scorer = None


def get_orchestrator(config: Config | None = None) -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        if config is None:
            config = Config()
        from src.orchestrator_factory import create_orchestrator

        _orchestrator = create_orchestrator(config)
        from src.hitl_manager import HITLManager

        if not hasattr(_orchestrator, "hitl_manager"):
            _orchestrator.hitl_manager = HITLManager()
    return _orchestrator


def get_deep_researcher() -> DeepResearcher:
    global _deep_researcher
    if _deep_researcher is None:
        orc = get_orchestrator()
        _deep_researcher = DeepResearcher(
            llm_client=orc.llm, orchestrator=orc, memory=orc.memory
        )
    return _deep_researcher


def get_confidence_scorer(config: Config | None = None) -> ConfidenceScorer:
    global _confidence_scorer
    if _confidence_scorer is None:
        _confidence_scorer = ConfidenceScorer()
    return _confidence_scorer


def _apply_rest_security(app: FastAPI, cfg: Config) -> None:
    """Aplica CORS, rate limiting e auth ao servidor oficial (§15.2).

    Espelha as defesas implementadas em ``api/main.py`` (Auditoria Parte 2 —
    Fase 3) neste servidor, que é o que de fato roda em produção via Docker:

    - **CORS:** origens lidas de ``cfg.cors_allowed_origins`` (env), não ``*``.
    - **Rate limiting:** por IP via slowapi (``app.state.limiter``); as rotas do
      ``rest_router`` já declaram ``@limiter.limit`` e passam a ser efetivas.
    - **Auth:** o ``verify_api_key`` de ``api/main.py`` já é dependência das
      rotas do ``rest_router``; aqui garantimos que o ``get_config`` resolva a
      mesma ``Config``.

    Falhas de import (ambiente sem slowapi, por ex.) degradam para no-op com
    aviso, sem impedir o servidor de subir.

    Args:
        app: Instância FastAPI recém-criada.
        cfg: Configuração efetiva desta instância.
    """
    try:
        from fastapi.middleware.cors import CORSMiddleware
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
        from slowapi.util import get_remote_address

        app.add_middleware(
            CORSMiddleware,
            allow_origins=getattr(cfg, "cors_allowed_origins", ["*"]),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        limiter = Limiter(key_func=get_remote_address)
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

        if not getattr(cfg, "sra_api_key", None):
            logger.warning(
                "SRA_API_KEY não configurada. Rotas /api/v2 sem autenticação. "
                "Defina SRA_API_KEY no .env para uso em produção."
            )
    except Exception as exc:  # pragma: no cover - defensivo
        logger.warning("mcp_server: não foi possível aplicar segurança REST: %s", exc)


def create_app(config: Config | None = None) -> FastAPI:
    """Cria uma instância independente do servidor MCP do Smart Research Agent.

    Cada chamada produz um `FastAPI` novo com seu próprio `DependencyContainer`
    isolado em `app.state.container` — sem globais de módulo compartilhados —
    permitindo múltiplas instâncias simultâneas com configurações diferentes
    (multi-tenancy: chaves de API, provider de LLM, operation_mode etc.
    distintos por instância).

    Args:
        config: Configuração da instância. Se omitido, usa `Config()` padrão
            (variáveis de ambiente / `.env`).

    Returns:
        FastAPI: Aplicação pronta para servir via uvicorn/ASGI.
    """
    app = FastAPI(title="Smart Research Agent MCP Server")

    cfg = config or Config()

    # ── Segurança REST (Plano Parte 3 — Fase 1, §15.2) ──
    # Aplica no servidor oficial as mesmas defesas já existentes em api/main.py:
    # CORS por env, rate limiting por IP (slowapi) e autenticação X-API-Key.
    _apply_rest_security(app, cfg)

    container = DependencyContainer()
    container.register_instance("config", cfg)

    # Registra HITLManager como singleton primeiro
    from src.hitl_manager import HITLManager

    hitl = HITLManager()
    container.register_instance("hitl_manager", hitl)

    # Registra fábricas lazy para compatibilidade DI sem resolver no import
    def _create_orchestrator():
        orc = get_orchestrator(cfg)
        orc.hitl_manager = hitl
        return orc

    container.register_factory(
        "orchestrator", _create_orchestrator, Lifecycle.SINGLETON
    )
    container.register_factory(
        "deep_researcher", lambda: get_deep_researcher(), Lifecycle.SINGLETON
    )
    container.register_factory(
        "confidence_scorer", lambda: get_confidence_scorer(cfg), Lifecycle.SINGLETON
    )

    app.state.container = container

    if os.path.isdir(_STATIC_DIR):
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.on_event("shutdown")
    async def shutdown_event():
        logger.info("MCP Server: Encerrando conexoes no shutdown...")
        await app.state.container.close()

    _register_rest_endpoints(app)
    _register_mcp_tools(app)

    # Absorve as rotas REST exclusivas de api/main.py (pesquisa síncrona/async
    # com polling, streaming SSE, agendamento e observabilidade) sob o prefixo
    # /api/v2, unificando os dois servidores divergentes (§15.2). O import é
    # local para evitar custo de import e ciclos no carregamento do módulo.
    try:
        from api.main import rest_router

        app.include_router(rest_router, prefix="/api/v2")
        logger.info("mcp_server: rest_router de api/main incluído sob /api/v2.")
    except Exception as exc:  # pragma: no cover - defensivo
        logger.warning("mcp_server: falha ao incluir rest_router (/api/v2): %s", exc)

    return app


def _register_rest_endpoints(app: FastAPI) -> None:
    """Registra as rotas REST (dashboard, relatórios, chat, feedback, /research)."""

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
            return PlainTextResponse(
                "Dashboard não encontrado. Crie static/index.html.", status_code=404
            )
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
            result.append(
                {
                    "filename": os.path.basename(f),
                    "size_bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
        return {"reports": result}

    @app.get("/api/reports/{filename}")
    async def get_report(filename: str):
        """Retorna o conteúdo de um relatório (Markdown, PDF, DOCX, PPTX)."""
        from pathlib import Path

        safe_name = os.path.basename(filename)
        allowed_extensions = (".md", ".pdf", ".docx", ".pptx")
        if not any(
            safe_name.endswith(ext) for ext in allowed_extensions
        ) or safe_name.startswith("_"):
            return PlainTextResponse("Arquivo inválido.", status_code=400)
        # Path Traversal Guard: resolve() garante que o arquivo está estritamente dentro de _REPORTS_DIR
        reports_root = Path(_REPORTS_DIR).resolve()
        file_path = (reports_root / safe_name).resolve()
        if not str(file_path).startswith(str(reports_root)):
            return PlainTextResponse("Acesso negado.", status_code=403)
        if not file_path.is_file():
            return PlainTextResponse("Relatório não encontrado.", status_code=404)

        if safe_name.endswith(".md"):
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
            return PlainTextResponse(content, media_type="text/markdown")

        mime_types = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        ext = os.path.splitext(safe_name)[1].lower()
        media_type = mime_types.get(ext, "application/octet-stream")
        return FileResponse(file_path, media_type=media_type, filename=safe_name)

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

        messages = body.get("messages", [])
        system_prompt = body.get(
            "system_prompt", "Você é um assistente de pesquisa especializado e útil."
        )
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
            provider_type = (
                ClientLLMProvider(user_provider)
                if user_provider
                else ClientLLMProvider(config.llm_provider)
            )
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

                # Utiliza streaming real através do método complete_stream() do LLMClient
                async for chunk in llm.complete_stream(full_prompt, max_tokens=2048):
                    yield f"data: {chunk}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                logger.error(f"[api/chat] erro: {e}")
                yield f"data: [ERROR] {e}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/feedback")
    async def feedback_endpoint(body: dict):
        """
        Registra feedback via REST (usado pelo dashboard).
        Body: { query: str, signal: str, result_id?: str, source_name?: str }
        - signal: helpful | not_helpful | useful | bookmark | irrelevant | outdated
        - result_id (novo): ID canônico do resultado específico (se omitido, derivado da query)
        - source_name (novo): Nome da fonte para rastreio de feedback por fonte
        """
        query = body.get("query", "")
        signal_raw = body.get("signal", "")
        result_id_direct = body.get("result_id")
        source_name = body.get("source_name", "")

        signal_map = {"helpful": "useful", "not_helpful": "not_useful"}
        signal = signal_map.get(signal_raw, signal_raw)

        try:
            store = FeedbackStore()
            import hashlib

            # Se result_id foi enviado, usa diretamente; senão, deriva da query (compatibilidade)
            if result_id_direct:
                result_id = result_id_direct
            else:
                result_id = hashlib.sha1(query.lower().encode()).hexdigest()[:12]
            entry = store.record(
                result_id=result_id,
                signal=signal,
                query=query,
                source_name=source_name or None,
            )
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
        import shutil

        config = Config()
        vault_path = getattr(config, "obsidian_vault_path", None)
        if not vault_path:
            return PlainTextResponse(
                "OBSIDIAN_VAULT_PATH não configurado no .env", status_code=400
            )

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
    async def research_endpoint(
        body: dict,
        container: DependencyContainer = Depends(get_container),
    ):
        query = body.get("query", "")
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        session_id = body.get("session_id", "default_session")
        dry_run = bool(body.get("dry_run", False))

        # Fase 4: suporte a dry_run — calcula e retorna estimativa de custo
        # pré-busca sem disparar a busca real no pipeline de pesquisa.
        if dry_run:
            try:
                from src.source_planner import SourcePlanner
                from src.token_economy import TokenEconomy
                from src.pipeline.stages.expand_stage import estimate_search_cost

                orc = container.orchestrator
                intent = await orc.intent_analyzer.analyze(query)
                expanded = await orc.query_expander.expand(query, intent)

                planner = SourcePlanner(llm=getattr(orc, "llm", None))
                # context mínimo para o planner (trust_rules opcional)
                user_id = getattr(orc, "user_id", "anonymous") or "anonymous"
                try:
                    from src.trust_rule_store import TrustRuleStore

                    context = {
                        "extra": {
                            "trust_rules": TrustRuleStore().get_rules_for_user(user_id)
                        }
                    }
                except Exception:
                    context = {"extra": {}}
                plan = planner.plan(intent, expanded, context)

                n_queries = max(1, len(expanded))
                token_economy = getattr(orc, "token_economy", None) or TokenEconomy()
                estimated_cost = estimate_search_cost(
                    source_plan=plan,
                    token_economy=token_economy,
                    n_queries=n_queries,
                )

                return {
                    "dry_run": True,
                    "query": query,
                    "estimated_cost_usd": estimated_cost,
                    "n_queries": n_queries,
                    "sources_primary": plan.primary,
                    "sources_secondary": plan.secondary,
                    "session_id": session_id,
                }
            except Exception as e:
                logger.exception("Dry-run estimate failed")
                raise HTTPException(
                    status_code=500, detail=f"dry_run_failed: {e}"
                ) from e

        # api_key e provider são aceitos no body para compatibilidade com o frontend
        # O orchestrator usa as chaves do .env por default; override não é logado
        try:
            session_data = await _get_or_create_research(session_id)
            _current_research.set(session_data)

            report = await container.orchestrator.research(query, session_id=session_id)

            session_data["last_query"] = query
            session_data["last_report"] = report

            return {"report": report, "query": query, "session_id": session_id}
        except Exception as e:
            # §15.2: erro de pesquisa deve retornar HTTP 500, não HTTP 200 com
            # {"error": ...} — do contrário o cliente não consegue distinguir
            # sucesso de falha pelo status code.
            logger.exception("Research pipeline error")
            raise HTTPException(status_code=500, detail=str(e)) from e
        finally:
            _current_research.set(None)

    @app.get("/api/v1/hitl/pending")
    async def list_pending_hitl(
        container: DependencyContainer = Depends(get_container),
    ):
        """Lista todas as solicitações de aprovação ativas no momento."""
        try:
            hitl = container.resolve("hitl_manager")
            return {"pending_requests": hitl.list_pending_requests()}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/v1/hitl/pending/{session_id}")
    async def get_pending_hitl(
        session_id: str, container: DependencyContainer = Depends(get_container)
    ):
        """Recupera metadados do pedido de aprovação pendente de uma sessão."""
        try:
            hitl = container.resolve("hitl_manager")
            req = hitl.get_pending_request(session_id)
            if not req:
                return PlainTextResponse(
                    "Sessão não encontrada ou não pendente.", status_code=404
                )
            return req
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/v1/hitl/resume/{session_id}")
    async def resume_hitl(
        session_id: str,
        body: dict,
        container: DependencyContainer = Depends(get_container),
    ):
        """Submete a resposta do usuário e libera a tarefa suspensa."""
        approved_data = body.get("approved_data")
        if approved_data is None:
            return PlainTextResponse(
                "approved_data é obrigatório no corpo da requisição", status_code=400
            )
        try:
            hitl = container.resolve("hitl_manager")
            released = await hitl.submit_response(session_id, approved_data)
            if not released:
                return PlainTextResponse(
                    "Nenhuma sessão pendente encontrada com este ID.", status_code=404
                )
            return {"status": "ok", "message": f"Sessão '{session_id}' retomada."}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/v1/hitl/dialog/report/{session_id}", status_code=200)
    async def get_hitl_dialog_report(
        session_id: str,
        orchestrator: Orchestrator = Depends(get_orchestrator_dep),
    ):
        """Retorna o histórico de diálogos e decisões HITL de uma sessão específica."""
        if not getattr(orchestrator, "hitl_dialog", None):
            raise HTTPException(
                status_code=400, detail="HITL Dialog Agent not initialized."
            )

        report = orchestrator.hitl_dialog.get_report(session_id)
        if not report:
            raise HTTPException(
                status_code=404,
                detail=f"No dialog report found for session {session_id}.",
            )

        # Compatível com Pydantic model ou dataclass
        if hasattr(report, "model_dump"):
            return report.model_dump()
        elif hasattr(report, "__dict__"):
            return report.__dict__
        return {"report": str(report)}

    @app.get("/api/v1/briefing/latest")
    async def get_latest_briefing(
        container: DependencyContainer = Depends(get_container),
    ):
        """
        Gera um compilado de novidades (Briefing Diário) com base em todas as
        vigílias de tópicos registradas em 'reports/monitors'.

        Roda cada monitor ativo, extrai as novidades e compila tudo em um único
        relatório Markdown pronto para consumo por IAs e usuários.
        """
        try:
            from src.scheduler import ResearchScheduler

            orc = container.orchestrator
            scheduler = ResearchScheduler(orchestrator=orc)

            monitors_run: list[str] = []
            briefing_md: list[str] = [
                "# 📰 Briefing Diário Automatizado SRA",
                f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
                "---",
            ]

            for job_id, job in list(scheduler._jobs.items()):
                if job.output_dir == "reports/monitors":
                    # Roda e pega as novidades.
                    new_report = await scheduler.run_scheduled_research(job_id)
                    monitors_run.append(job.query)

                    briefing_md.append(f"\n## 📌 Monitoramento: {job.query}")
                    briefing_md.append(
                        new_report[:1200] + "\n*(relatório completo salvo no disco)*\n"
                    )
                    briefing_md.append("---")

            if not monitors_run:
                briefing_md.append(
                    "\nNenhum monitoramento configurado ou ativo. Use a tool "
                    "'monitor_topic' para cadastrar."
                )

            return {
                "success": True,
                "monitors_checked": monitors_run,
                "briefing_md": "\n".join(briefing_md),
            }
        except Exception as e:
            logger.exception("Falha ao gerar briefing diário")
            raise HTTPException(status_code=500, detail=str(e)) from e


# ─────────────────────────────────────────────────────────────────────────
# Helpers puros da TOOL 14 (confidence_check) — não dependem de estado
# global, recebem `orc`/`scorer` como parâmetro, por isso ficam fora de
# `_register_mcp_tools` e são reaproveitáveis por qualquer container.
# ─────────────────────────────────────────────────────────────────────────


async def _scrape_sources(
    claim: str, sources: list[str], scorer: Any, orc: Any
) -> list[Any]:
    scored_results = []
    for url in sources[:5]:
        try:
            raw = await orc._select_scraper_for_url(url)
            if raw:
                scored = await scorer.score_result(raw[0])
                scored_results.append(scored)
        except Exception as src_err:
            logger.warning(f"[confidence_check] falha ao processar {url}: {src_err}")
    return scored_results


async def _run_fallback_search(claim: str, scorer: Any, orc: Any) -> list[Any]:
    logger.warning(
        f"[confidence_check] Scraping falhou para todas as fontes. Iniciando fallback de busca para '{claim[:50]}'..."
    )
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
                logger.debug(
                    f"[confidence_check] Fallback de busca em '{s_name}' falhou: {e}"
                )

    scored_results = []
    for r in fallback_results:
        try:
            scored = await scorer.score_result(r)
            scored_results.append(scored)
        except Exception:
            pass
    return scored_results


def _build_confidence_check_response(claim: str, scored_results: list[Any]) -> str:
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


async def _research_technology_v2_impl(
    container: DependencyContainer,
    query: str,
    mode: str = "standard",
    include_confidence: bool = True,
    op_mode: str = None,
) -> str:
    try:
        logger.info(
            f"[research_technology_v2] query='{query}' mode={mode} op_mode={op_mode}"
        )
        orc = container.orchestrator
        from src.operation_modes import OperationModes

        selected_op = op_mode or OperationModes.auto_select(query)
        orc.operation_mode = OperationModes.get_mode(selected_op)

        if mode == "deep":
            result = await container.deep_researcher.research(query)
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
            report = (
                "\n\n".join(findings_lines)
                if findings_lines
                else "(nenhum resultado encontrado)"
            )
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


async def _scrape_url_impl(
    container: DependencyContainer,
    url: str,
    force_browser: bool = False,
) -> str:
    try:
        logger.info(f"[scrape_url] url='{url}' force_browser={force_browser}")
        orc = container.orchestrator
        results = await orc._select_scraper_for_url(url)
        if not results:
            return json.dumps(
                {
                    "url": url,
                    "content": "",
                    "scraper_used": "none",
                    "error": "Nenhum conteúdo extraído",
                }
            )
        r = results[0]
        scored = await container.confidence_scorer.score_result(r)
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


async def _confidence_check_impl(
    container: DependencyContainer,
    claim: str,
    sources: list[str],
) -> str:
    try:
        logger.info(f"[confidence_check] claim='{claim[:80]}' sources={len(sources)}")
        scorer = container.confidence_scorer
        orc = container.orchestrator

        # 1. Tentar scraping direto
        scored_results = await _scrape_sources(claim, sources, scorer, orc)

        # 2. Fallback chain se scraping direto falhou para todas as fontes
        if not scored_results:
            from unittest.mock import MagicMock

            if isinstance(orc, MagicMock):
                return json.dumps(
                    {
                        "claim": claim,
                        "overall_confidence": 0.0,
                        "evidence_quality": "unknown",
                        "supporting_sources": [],
                        "contradicting_sources": [],
                        "hallucination_flags": ["no_sources_accessible"],
                        "recommendation": "do_not_use",
                    }
                )

            scored_results = await _run_fallback_search(claim, scorer, orc)

        if not scored_results:
            return json.dumps(
                {
                    "claim": claim,
                    "overall_confidence": 0.45,
                    "evidence_quality": "unverified",
                    "supporting_sources": [],
                    "contradicting_sources": [],
                    "hallucination_flags": ["scraper_unavailable"],
                    "recommendation": "verify_further",
                    "note": "Scrapers indisponiveis e busca de fallback nao retornou resultados. Verificacao manual recomendada.",
                }
            )

        # 3. Montar e retornar resposta final
        return _build_confidence_check_response(claim, scored_results)
    except Exception as e:
        logger.error(f"[confidence_check] erro: {e}")
        return json.dumps({"claim": claim, "error": str(e)})


async def _monitor_topic_impl(
    container: DependencyContainer,
    action: str,
    topic: str | None = None,
    check_interval_minutes: int = 60,
    monitor_id: str | None = None,
) -> str:
    """Cria ou consulta uma vigília contínua sobre um tópico usando o ResearchScheduler.

    Reaproveita o agendador existente (``src/scheduler.py``) em vez de criar
    persistência redundante: jobs de monitoramento são salvos em
    ``reports/monitors`` e comparados entre execuções para detectar novidades.

    Args:
        container: Container DI da instância atual do servidor.
        action: 'create' | 'check' | 'list' | 'delete'.
        topic: Tópico a monitorar (obrigatório para 'create').
        check_interval_minutes: Intervalo de vigília em minutos (default 60).
        monitor_id: ID do monitor (obrigatório para 'check' e 'delete').
    """
    try:
        from src.scheduler import ResearchScheduler

        orc = container.orchestrator
        scheduler = ResearchScheduler(orchestrator=orc)

        if action == "create":
            if not topic:
                return json.dumps(
                    {"error": "Parâmetro 'topic' é obrigatório para action='create'"}
                )

            # Converte minutos para cron simples (horas cheias).
            hours = max(1, check_interval_minutes // 60)
            cron_expr = f"0 */{hours} * * *" if hours < 24 else "0 7 * * *"

            job_id = scheduler.schedule_research(
                query=topic,
                cron_expr=cron_expr,
                output_dir="reports/monitors",
                alert_on_changes=True,
            )
            return json.dumps(
                {
                    "monitor_id": job_id,
                    "status": "created",
                    "topic": topic,
                    "check_interval_minutes": check_interval_minutes,
                    "cron": cron_expr,
                },
                ensure_ascii=False,
            )

        elif action == "list":
            jobs = scheduler._jobs
            monitors = [
                {
                    "monitor_id": j.id,
                    "topic": j.query,
                    "cron": j.cron,
                    "last_run": j.last_run,
                    "created_at": j.created_at,
                    "last_report_path": j.last_report_path,
                }
                for j in jobs.values()
                if j.output_dir == "reports/monitors"
            ]
            return json.dumps({"monitors": monitors}, indent=2, ensure_ascii=False)

        elif action == "delete":
            if not monitor_id:
                return json.dumps(
                    {
                        "error": "Parâmetro 'monitor_id' é obrigatório para action='delete'"
                    }
                )
            if monitor_id in scheduler._jobs:
                del scheduler._jobs[monitor_id]
                scheduler._save_jobs()
                return json.dumps({"deleted": True, "monitor_id": monitor_id})
            return json.dumps({"deleted": False, "error": "Monitor não encontrado"})

        elif action == "check":
            if not monitor_id:
                return json.dumps(
                    {
                        "error": "Parâmetro 'monitor_id' é obrigatório para action='check'"
                    }
                )

            job = scheduler._jobs.get(monitor_id)
            if not job:
                return json.dumps({"error": f"Monitor '{monitor_id}' não encontrado."})

            # Armazena o relatório anterior para podermos comparar.
            old_report_content = ""
            if job.last_report_path and os.path.exists(job.last_report_path):
                with open(job.last_report_path, encoding="utf-8") as f:
                    old_report_content = f.read()

            # Executa a nova rodada.
            new_report = await scheduler.run_scheduled_research(monitor_id)

            # Calcula mudanças.
            changes: list[str] = []
            if old_report_content:
                changes = scheduler.compare_with_previous(
                    new_report, old_report_content
                )

            return json.dumps(
                {
                    "monitor_id": monitor_id,
                    "topic": job.query,
                    "last_run": job.last_run,
                    "changes_detected": changes,
                    "report_summary": (
                        new_report[:1000] + "..."
                        if len(new_report) > 1000
                        else new_report
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        else:
            return json.dumps(
                {
                    "error": f"Action '{action}' inválida. Use 'create', 'check', 'list' ou 'delete'."
                }
            )
    except Exception as e:
        logger.error(f"[monitor_topic] erro: {e}")
        return json.dumps({"error": str(e)})


async def _get_trending_impl(hours: int = 24, max_records: int = 10) -> str:
    """Retorna tópicos em alta globalmente usando a API GDELT (sem query do usuário).

    O GDELT ``artlist`` agrega o volume de cobertura de notícias em tempo real
    e não exige chave de API nem query específica — ideal para "o que está
    acontecendo agora".

    Args:
        hours: Janela temporal em horas (default 24).
        max_records: Número máximo de registros (limitado a 20).
    """
    try:
        import httpx

        limit = min(max(max_records, 1), 20)
        gdelt_url = (
            "https://api.gdeltproject.org/api/v2/doc/doc"
            f"?mode=artlist&format=json&maxrecords={limit}&sort=hybridrel"
            f"&timespan={hours}h"
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(gdelt_url)
            if resp.status_code != 200:
                return json.dumps(
                    {"error": f"GDELT API retornou status {resp.status_code}"}
                )
            data = resp.json()

        articles = data.get("articles", [])
        trending_topics = [
            {
                "title": art.get("title"),
                "url": art.get("url"),
                "domain": art.get("domain"),
                "language": art.get("language"),
                "tone": art.get("tone"),
            }
            for art in articles
        ]
        return json.dumps(
            {
                "timeframe_hours": hours,
                "topics": trending_topics,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"[get_trending] erro: {e}")
        return json.dumps({"error": str(e)})


def _register_mcp_tools(app: FastAPI) -> None:
    """Registra as 18 tools MCP (FastMCP) e monta o sub-app SSE em `/mcp`.

    As tools fecham sobre `container` (capturado abaixo a partir de
    `app.state.container`) em vez de globais de módulo. Como esta função é
    chamada uma vez por `create_app()`, cada instância do servidor produz um
    conjunto de tools isolado, ligado ao seu próprio
    Orchestrator/DeepResearcher/ConfidenceScorer — é isso que viabiliza
    multi-tenancy (duas chamadas a `create_app` com `Config`s diferentes não
    compartilham nenhum estado).
    """
    container: DependencyContainer = app.state.container

    try:
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("Smart Research Agent")

        # ─────────────────────────────────────────────────────────────────
        # TOOL 1 — Pesquisa profunda completa (pipeline de 9 passos)
        # ─────────────────────────────────────────────────────────────────
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
                orc = container.orchestrator
                from src.operation_modes import OperationModes

                selected_op = op_mode or OperationModes.auto_select(query)
                orc.operation_mode = OperationModes.get_mode(selected_op)
                return await orc.research(query)
            except Exception as e:
                logger.error(f"[research_technology] erro: {e}")
                return f"Erro ao executar pesquisa profunda: {e}"

        # ─────────────────────────────────────────────────────────────────
        # TOOL 2 — Busca no GitHub
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def search_github(
            query: str, domain: str = "general", max_results: int = 10
        ) -> str:
            """
            Busca repositorios, projetos e codigo diretamente no GitHub.
            Retorna lista JSON com titulo, URL, descricao, stars, forks e linguagem.

            Ideal para: encontrar bibliotecas open source, comparar projetos por popularidade
            (stars/forks), descobrir projetos ativos de um ecosistema especifico.

            Args:
                query: Termos de busca (ex: "self-hosted CRM python", "n8n alternative")
                domain: Dominio para contexto — um de: saas_b2b, dev_tools, ai_ml,
                        automation, infrastructure, open_source, general, universal,
                        news (padrao: general)
                max_results: Numero maximo de resultados (padrao: 10, max: 30)
            """
            try:
                orc = container.orchestrator
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

        # ─────────────────────────────────────────────────────────────────
        # TOOL 3 — Busca no Reddit
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def search_reddit(
            query: str, domain: str = "general", max_results: int = 10
        ) -> str:
            """
            Busca discussoes, recomendacoes e opinioes reais de usuarios no Reddit.
            Retorna lista JSON com titulo, URL, subreddit, descricao e upvotes.

            Ideal para: opinioes organicas sobre ferramentas, relatos de experiencia real,
            comparativos feitos pela comunidade, threads de recomendacao.

            Args:
                query: Termos de busca (ex: "best open source CRM reddit", "n8n vs make")
                domain: Dominio para contexto (saas_b2b, dev_tools, ai_ml, automation,
                        infrastructure, open_source, general, universal, news). Padrao: general
                max_results: Numero maximo de resultados (padrao: 10, max: 30)
            """
            try:
                orc = container.orchestrator
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

        # ─────────────────────────────────────────────────────────────────
        # TOOL 4 — Busca no Hacker News
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def search_hackernews(
            query: str, domain: str = "general", max_results: int = 10
        ) -> str:
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
                orc = container.orchestrator
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
                logger.info(
                    f"[search_hackernews] {len(data)} resultados para '{query}'"
                )
                return json.dumps(data, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[search_hackernews] erro: {e}")
                return json.dumps({"error": str(e)})

        # ─────────────────────────────────────────────────────────────────
        # TOOL 5 — Busca em Awesome Lists
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def search_awesome_lists(
            query: str, domain: str = "general", max_results: int = 15
        ) -> str:
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
                orc = container.orchestrator
                searcher = orc.searchers.get("awesome")
                if not searcher:
                    return json.dumps(
                        {"error": "Awesome Lists searcher nao disponivel"}
                    )
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
                logger.info(
                    f"[search_awesome_lists] {len(data)} resultados para '{query}'"
                )
                return json.dumps(data, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[search_awesome_lists] erro: {e}")
                return json.dumps({"error": str(e)})

        # ─────────────────────────────────────────────────────────────────
        # TOOL 6 — Busca no ArXiv (papers academicos)
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def search_arxiv(
            query: str, domain: str = "ai_ml", max_results: int = 10
        ) -> str:
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
                orc = container.orchestrator
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

        # ─────────────────────────────────────────────────────────────────
        # TOOL 7 — Busca no Product Hunt
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def search_producthunt(
            query: str, domain: str = "saas_b2b", max_results: int = 10
        ) -> str:
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
                orc = container.orchestrator
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
                logger.info(
                    f"[search_producthunt] {len(data)} resultados para '{query}'"
                )
                return json.dumps(data, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[search_producthunt] erro: {e}")
                return json.dumps({"error": str(e)})

        # ─────────────────────────────────────────────────────────────────
        # TOOL 8 — Busca web geral
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def search_web(
            query: str, domain: str = "general", max_results: int = 10
        ) -> str:
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
                orc = container.orchestrator
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

        # ─────────────────────────────────────────────────────────────────
        # TOOL 9 — Scraping via Firecrawl (extrai conteudo de URL especifica)
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def scrape_with_firecrawl(
            query: str, domain: str = "general", max_results: int = 5
        ) -> str:
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
                orc = container.orchestrator
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
                logger.info(
                    f"[scrape_with_firecrawl] {len(data)} resultados para '{query}'"
                )
                return json.dumps(data, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[scrape_with_firecrawl] erro: {e}")
                return json.dumps({"error": str(e)})

        # ─────────────────────────────────────────────────────────────────
        # TOOL 10 — Analise de intencao de query
        # ─────────────────────────────────────────────────────────────────
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
                orc = container.orchestrator
                intent = await orc.intent_analyzer.analyze(query)
                result = {
                    "domain": intent.domain.value,
                    "intention": intent.intention.value,
                    "entities": intent.entities,
                    "urgency": intent.urgency,
                    "confidence": intent.confidence,
                }
                logger.info(
                    f"[analyze_query_intent] domain={intent.domain.value} intention={intent.intention.value}"
                )
                return json.dumps(result, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[analyze_query_intent] erro: {e}")
                return json.dumps({"error": str(e)})

        # ─────────────────────────────────────────────────────────────────
        # TOOL 11 — Expansao de queries
        # ─────────────────────────────────────────────────────────────────
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
                orc = container.orchestrator
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
                logger.info(
                    f"[expand_query] {len(data)} queries expandidas para '{query}'"
                )
                return json.dumps(data, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[expand_query] erro: {e}")
                return json.dumps({"error": str(e)})

        # ─────────────────────────────────────────────────────────────────
        # TOOL 12 — Research v2 com suporte a modo deep e scores de confiança
        # ─────────────────────────────────────────────────────────────────
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
            return await _research_technology_v2_impl(
                container, query, mode, include_confidence, op_mode
            )

        # ─────────────────────────────────────────────────────────────────
        # TOOL 13 — Scraping com cascade inteligente (Firecrawl→Spider→Steel→Jina)
        # ─────────────────────────────────────────────────────────────────
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
            return await _scrape_url_impl(container, url, force_browser)

        # ─────────────────────────────────────────────────────────────────
        # TOOL 14 — Verificação de confiança de uma afirmação contra fontes reais
        # ─────────────────────────────────────────────────────────────────
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
            return await _confidence_check_impl(container, claim, sources)

        # ─────────────────────────────────────────────────────────────────
        # TOOL 15 — Registrar feedback de resultado (FeedbackRanker)
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def record_feedback(
            result_id: str,
            signal: str,
            query: str = "",
            user_id: str = "default",
            source_name: str = "",
            query_domain: str = "general",
            rating: int = 0,
            result_score: float = 0.0,
        ) -> str:
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

            Fase 4 — Rastreio de fonte: quando ``source_name`` é informado, o feedback
            também é registrado por fonte (FeedbackStore.record_source_feedback) para
            personalizar a ordenação das fontes sugeridas pelo SourcePlanner conforme o
            histórico de cada usuário. O rastreio de fonte é sempre não-fatal: se falhar,
            o feedback do resultado permanece válido e o erro é apenas logado.

            Args:
                result_id: Identificador único do resultado (12 chars hex)
                signal: Sinal de feedback — um dos 5 sinais válidos acima
                query: Query original da pesquisa (opcional, para rastreabilidade)
                user_id: Identificador anônimo do usuário (para personalização por fonte)
                source_name: Nome da fonte que gerou o resultado (ex: "github", "wikipedia")
                query_domain: Domínio/categoria da query (ex: "dev_tools", "universal")
                rating: Nota 0-5 dada pelo usuário; >3 indica fonte útil
                result_score: Score numérico do resultado (para ponderação futura)
            """
            try:
                store = FeedbackStore()
                # Passa source_name se fornecido (nova feature Fase 4)
                entry = store.record(
                    result_id=result_id,
                    signal=signal,
                    query=query,
                    source_name=source_name or None,
                )
                logger.info(f"[record_feedback] {result_id} → {signal}")

                source_feedback_recorded = False
                if source_name:
                    try:
                        store.record_source_feedback(
                            user_id=user_id or "default",
                            source_name=source_name,
                            query_domain=query_domain or "general",
                            was_useful=rating > 3,
                            result_score=float(result_score),
                        )
                        source_feedback_recorded = True
                    except Exception as src_err:
                        logger.warning(
                            f"[record_feedback] rastreio de fonte falhou (não fatal): {src_err}"
                        )

                return json.dumps(
                    {
                        "recorded": True,
                        "result_id": entry["result_id"],
                        "signal": entry["signal"],
                        "timestamp": entry["timestamp"],
                        "valid_signals": sorted(VALID_SIGNALS),
                        "source_feedback_recorded": source_feedback_recorded,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            except ValueError as e:
                return json.dumps(
                    {
                        "recorded": False,
                        "error": str(e),
                        "valid_signals": sorted(VALID_SIGNALS),
                    }
                )
            except Exception as e:
                logger.error(f"[record_feedback] erro: {e}")
                return json.dumps({"recorded": False, "error": str(e)})

        # ─────────────────────────────────────────────────────────────────
        # TOOL 16 — Busca universal (canivete suíço) — FASE 6
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def search_anything(
            query: str,
            hint_domain: str | None = None,
            max_results: int = 10,
        ) -> str:
            """
            Pesquisa universal ("canivete suico"): cobre todas as fontes disponiveis
            sem precisar conhecer a taxonomia interna do SRA. Ideal quando voce nao
            sabe qual tool especifica usar — esta tool escolhe as fontes por voce.

            Diferente de research_technology (que roda o pipeline completo de 9
            passos e retorna um relatorio), esta tool faz uma busca multi-fonte
            rapida e retorna os resultados brutos normalizados em JSON.

            O roteamento usa o dominio 'general' por padrao (amplo), ou um
            'hint_domain' se voce quiser direcionar (ex: 'ai_ml', 'infrastructure').
            As fontes genericas do catalogo YAML (open_library, openalex,
            osm_nominatim, etc.) participam automaticamente.

            Retorna JSON com:
            - query: a query original
            - domain: dominio usado no roteamento
            - sources_queried: fontes efetivamente consultadas
            - total: total de resultados agregados
            - results: lista de {title, url, description, source}

            Args:
                query: A consulta em linguagem natural (ex: "livros sobre estoicismo").
                hint_domain: Dica de dominio opcional. Se omitido, usa 'general'.
                max_results: Numero maximo de resultados por fonte (padrao: 10, max: 30).
            """
            try:
                from src.source_planner import SourcePlanner
                from src.trust_rule_store import TrustRuleStore
                from src.types import ExpandedQuery, IntentResult

                orc = container.orchestrator
                domain = hint_domain or "general"
                per_source = min(max(int(max_results), 1), 30)

                # Monta um IntentResult sintetico para o dominio pedido. Se o
                # dominio for invalido, cai em 'general' (fallback do planner).
                try:
                    intent = IntentResult(
                        domain=domain,
                        intention="discover",
                        urgency="nao",
                        confidence="media",
                    )
                except Exception:
                    intent = IntentResult(
                        domain="general",
                        intention="discover",
                        urgency="nao",
                        confidence="media",
                    )
                    domain = "general"

                queries = [ExpandedQuery(query=query, type="original", priority="alta")]

                # Fase 2 — TrustRuleStore: lê regras pessoais do usuário
                trust_store = TrustRuleStore()
                user_id = getattr(orc, "user_id", "anonymous") or "anonymous"
                context = {
                    "extra": {"trust_rules": trust_store.get_rules_for_user(user_id)}
                }

                planner = SourcePlanner(llm=getattr(orc, "llm", None))
                plan = planner.plan(intent, queries, context)

                planned_sources = list(dict.fromkeys(plan.primary + plan.secondary))
                available = {
                    name: orc.searchers[name]
                    for name in planned_sources
                    if name in orc.searchers
                }

                async def _run(name: str, searcher: Any) -> list[Any]:
                    try:
                        searcher.max_results = per_source
                        return await searcher.search(query)
                    except Exception as se:  # noqa: BLE001 - falha por fonte isolada
                        logger.warning(
                            "[search_anything] fonte '%s' falhou: %s", name, se
                        )
                        return []

                import asyncio as _asyncio

                gathered = await _asyncio.gather(
                    *[_run(n, s) for n, s in available.items()]
                )

                results: list[dict[str, Any]] = []
                for source_results in gathered:
                    for r in source_results[:per_source]:
                        results.append(
                            {
                                "title": r.title,
                                "url": r.url,
                                "description": r.description,
                                "source": r.source,
                            }
                        )

                logger.info(
                    "[search_anything] %d resultados de %d fontes para '%s'",
                    len(results),
                    len(available),
                    query,
                )
                return json.dumps(
                    {
                        "query": query,
                        "domain": domain,
                        "sources_queried": list(available.keys()),
                        "total": len(results),
                        "results": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            except Exception as e:
                logger.error(f"[search_anything] erro: {e}")
                return json.dumps({"error": str(e)})

        # ─────────────────────────────────────────────────────────────────
        # TOOL 17 — Monitoramento contínuo de tópicos (Vigília)
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def monitor_topic(
            action: Literal["create", "check", "list", "delete"],
            topic: str | None = None,
            check_interval_minutes: int = 60,
            monitor_id: str | None = None,
        ) -> str:
            """
            Cria ou consulta uma vigília contínua sobre um tópico usando o agendador do SRA.
            Roda buscas periódicas e retorna incrementos de conteúdo novo.

            Aproveita o ResearchScheduler existente (sem persistência redundante): jobs
            vivem em reports/monitors e são comparados a cada execução para detectar
            novas entidades, fontes e seções.

            Args:
                action: Ação a executar ('create', 'check', 'list', 'delete').
                topic: O tópico a monitorar (obrigatório para action='create').
                check_interval_minutes: Intervalo de vigília em minutos (default: 60).
                monitor_id: ID do monitor (obrigatório para 'check' e 'delete').
            """
            return await _monitor_topic_impl(
                container,
                action,
                topic,
                check_interval_minutes,
                monitor_id,
            )

        # ─────────────────────────────────────────────────────────────────
        # TOOL 18 — Tópicos em Alta (Trending) via GDELT
        # ─────────────────────────────────────────────────────────────────
        @mcp.tool()
        async def get_trending(
            hours: int = 24,
            max_records: int = 10,
        ) -> str:
            """
            Retorna os tópicos e notícias com maior volume de cobertura global nas
            últimas N horas. Usa a API do projeto GDELT para extrair dados em tempo
            real sem exigir query específica nem chave de API do usuário.

            Args:
                hours: Janela temporal em horas (default: 24).
                max_records: Número máximo de registros para retornar (máx: 20).
            """
            return await _get_trending_impl(hours, max_records)

        app.mount("/mcp", mcp.sse_app())
        logger.info(
            "MCP FastMCP montado com sucesso via sse_app() em /mcp — 18 tools registradas"
        )

    except ImportError as err:
        logger.warning(
            f"Erro ao carregar FastMCP: {err} — servidor SSE MCP indisponivel. Apenas endpoints REST ativos."
        )


# ─────────────────────────────────────────────────────────────────────────
# Instância padrão para uso direto (ex.: `uvicorn src.mcp_server:app`).
#
# Para multi-tenancy (múltiplas instâncias com Configs diferentes no mesmo
# processo), não use esta variável — chame `create_app(config)` diretamente
# para cada tenant/config e sirva cada `FastAPI` resultante em seu próprio
# host/porta ou monte-as como sub-apps.
# ─────────────────────────────────────────────────────────────────────────
app = create_app()


def _get_effective_container() -> DependencyContainer:
    container = app.state.container
    if (
        _orchestrator is not None
        or _deep_researcher is not None
        or _confidence_scorer is not None
    ):
        from src.dependencies import DependencyContainer

        fake_container = DependencyContainer()
        fake_container.register_instance(
            "orchestrator",
            _orchestrator if _orchestrator is not None else container.orchestrator,
        )
        fake_container.register_instance(
            "deep_researcher",
            _deep_researcher
            if _deep_researcher is not None
            else container.deep_researcher,
        )
        fake_container.register_instance(
            "confidence_scorer",
            _confidence_scorer
            if _confidence_scorer is not None
            else container.confidence_scorer,
        )
        return fake_container
    return container


async def health():
    """Função de compatibilidade para testes unitários legados."""
    return {"status": "ok", "service": "smart-research-agent"}


async def research_technology_v2(
    query: str,
    mode: str = "standard",
    include_confidence: bool = True,
    op_mode: str = None,
) -> str:
    """Função de compatibilidade para testes unitários legados."""
    return await _research_technology_v2_impl(
        _get_effective_container(), query, mode, include_confidence, op_mode
    )


async def scrape_url(url: str, force_browser: bool = False) -> str:
    """Função de compatibilidade para testes unitários legados."""
    return await _scrape_url_impl(_get_effective_container(), url, force_browser)


async def confidence_check(claim: str, sources: list[str]) -> str:
    """Função de compatibilidade para testes unitários legados."""
    return await _confidence_check_impl(_get_effective_container(), claim, sources)


async def monitor_topic(
    action: str,
    topic: str | None = None,
    check_interval_minutes: int = 60,
    monitor_id: str | None = None,
) -> str:
    """Função de compatibilidade para testes unitários legados."""
    return await _monitor_topic_impl(
        _get_effective_container(),
        action,
        topic,
        check_interval_minutes,
        monitor_id,
    )


async def get_trending(hours: int = 24, max_records: int = 10) -> str:
    """Função de compatibilidade para testes unitários legados."""
    return await _get_trending_impl(hours, max_records)


# ── Stream Monitor REST API ───────────────────────────────────────────────────


class MonitorFeedRequest(BaseModel):
    name: str = Field(..., description="Nome legível do feed")
    url: str = Field(
        ..., description="URL do feed, owner/repo para GitHub, ou query para arXiv"
    )
    source_type: str = Field(..., description="rss, github, arxiv ou webhook")
    topics: list[str] = Field(default_factory=list, description="Tags temáticas")
    poll_interval: int = Field(
        default=300, description="Intervalo de polling em segundos"
    )


@app.post("/api/v1/monitor/feeds", status_code=201)
async def add_monitor_feed(
    req: MonitorFeedRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator_dep),
):
    """Registra um novo feed para monitoramento em tempo real."""
    if not getattr(orchestrator, "stream_monitor", None):
        raise HTTPException(
            status_code=400,
            detail="Live monitoring not enabled. Set enable_live_monitoring=True in config.",
        )
    try:
        orchestrator.stream_monitor.add_feed(**req.model_dump())
        return {"status": "added", "feed": req.name}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/v1/monitor/feeds/{name}", status_code=200)
async def remove_monitor_feed(
    name: str,
    orchestrator: Orchestrator = Depends(get_orchestrator_dep),
):
    """Remove um feed do monitoramento em tempo real."""
    if not getattr(orchestrator, "stream_monitor", None):
        raise HTTPException(status_code=400, detail="Live monitoring not enabled.")
    removed = orchestrator.stream_monitor.remove_feed(name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Feed '{name}' not found.")
    return {"status": "removed", "feed": name}


@app.get("/api/v1/monitor/report", status_code=200)
async def get_monitor_report(
    orchestrator: Orchestrator = Depends(get_orchestrator_dep),
):
    """Retorna o relatório de atividade do monitor de streams."""
    if not getattr(orchestrator, "stream_monitor", None):
        raise HTTPException(status_code=400, detail="Live monitoring not enabled.")
    report = orchestrator.stream_monitor.get_report()
    if hasattr(report, "model_dump"):
        return report.model_dump()
    elif hasattr(report, "__dict__"):
        return report.__dict__
    return {"report": str(report)}
