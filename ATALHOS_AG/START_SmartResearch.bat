@echo off
setlocal enabledelayedexpansion

:: ============================================================
:: Smart Research Agent (SRA) - Launcher
:: Resolves repo path dynamically from script location (no hardcoded paths)
:: ============================================================
set "SRA_REPO=%~dp0..\smart-research-agent"
pushd "%SRA_REPO%" 2>nul
if errorlevel 1 (
    echo [ARES] [ERRO] Nao foi possivel localizar o repositorio smart-research-agent em: %SRA_REPO%
    echo [ARES] [ERRO] Execute este bat a partir da pasta ATALHOS_AG dentro de E:\Meus LLmos
    pause
    exit /b 1
)
:: Normaliza para caminho absoluto
for %%I in (.) do set "SRA_REPO=%%~fI"
popd

echo [ARES] Iniciando Smart Research Agent SRA-V3.0 (Nivel Supremo)...
echo [ARES] Repo: %SRA_REPO%
echo.

:: ============================================================
:: PASSO 0 - Limpeza de Portas (Evitar erros de Socket Ocupado)
:: ============================================================
echo [ARES] Verificando e liberando portas locais ocupadas...
:: Porta 3458: MCP Server / REST API oficial (SRA)
powershell -Command "Get-NetTCPConnection -LocalPort 3458 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; exit 0"
:: Porta 8001: Prometheus Metrics
powershell -Command "Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; exit 0"
:: Porta 8501: Streamlit Web UI (host)
powershell -Command "Get-NetTCPConnection -LocalPort 8501 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; exit 0"
:: Porta 6379: Redis (host)
powershell -Command "Get-NetTCPConnection -LocalPort 6379 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; exit 0"
echo [ARES] Portas liberadas com sucesso.
echo.

:: ============================================================
:: PASSO 0.5 - Verificacao do Docker (Requisito para Containers)
:: ============================================================
echo [ARES] Verificando se o Docker daemon esta ativo e respondendo...
docker ps >nul 2>&1
if !errorlevel! neq 0 (
    echo [ARES] [AVISO] Docker Desktop/Daemon nao esta respondendo ou nao instalado!
    echo [ARES] [AVISO] Containers Docker nao serao iniciados. SRA usar[a modo HOST.
    echo [ARES] [AVISO] Para stack completa: inicie o Docker Desktop e execute novamente.
    set DOCKER_AVAILABLE=false
) else (
    echo [ARES] Docker Daemon ativo e respondendo.
    set DOCKER_AVAILABLE=true
)
echo.

:: ============================================================
:: PASSO 1 - SRA MCP Server: Docker ou Host
:: ============================================================
:: O Firecrawl_New (START_Firecrawl.bat) consome ~12GB de RAM (api_new=8GB +
:: playwright-service=4GB). Com apenas 4GB livres, iniciar tudo de uma vez
:: faz o Docker Desktop ser morto pelo Windows (OOM). A estrategia aqui e:
::   1) Subir SRA Docker primeiro (mais leve: ~5GB)
::   2) Firecrawl pode ser iniciado manualmente via START_Firecrawl.bat quando necessario
:: ============================================================
cd /d "%SRA_REPO%"

set SRA_MODE=host

if "!DOCKER_AVAILABLE!"=="true" (
    :: Verificar/Criar a rede externa firecrawl_backend (obrigatoria pelo compose)
    docker network inspect firecrawl_backend >nul 2>&1
    if !errorlevel! neq 0 (
        echo [ARES] Criando rede firecrawl_backend (SRA pode usar fallback sem Firecrawl)...
        docker network create --driver bridge firecrawl_backend >nul 2>&1
    )

    echo [ARES] [1/4] Subindo stack Docker Compose (SRA + SearXNG + Redis + ChromaDB)...
    docker compose --profile dev --profile redis --profile chromadb up -d
    if !errorlevel! neq 0 (
        echo [ARES] [AVISO] docker compose falhou. Tentando docker-compose (v1)...
        docker-compose --profile dev --profile redis --profile chromadb up -d
    )

    if !errorlevel! equ 0 (
        echo [ARES] Containers Docker subidos com sucesso.
        set SRA_MODE=docker
        echo [ARES] Aguardando 15 segundos para os containers inicializarem...
        timeout /t 15 /nobreak >nul
    ) else (
        echo [ARES] [AVISO] Falha ao subir containers Docker. Usando modo HOST.
        set SRA_MODE=host
    )
)

:START_MCP_SERVER
echo [ARES] [1/4] Iniciando MCP Server (porta 3458)...
if "!SRA_MODE!"=="docker" (
    echo [ARES] [AVISO] Modo Docker: containers SRA/SearXNG/Redis/ChromaDB sobem via compose.
    echo [ARES] [AVISO] MCP Server roda dentro do container; abrindo CMD para monitoramento.
    start "SRA MCP Server Monitor" cmd /k "echo [ARES] MCP Server no container Docker - acesse http://localhost:3458 & echo [ARES] Para reiniciar: docker compose restart mcp-server & cmd /k"
) else (
    echo [ARES] [AVISO] Modo host: Redis/ChromaDB/SearXNG usam fallback em memoria.
    start "SRA MCP Server (Host Mode)" cmd /k "cd /d \"%SRA_REPO%\" && call .venv\Scripts\activate.bat && set HOST_MODE=true && uvicorn src.mcp_server:app --host 0.0.0.0 --port 3458"
    echo [ARES] Aguardando 10 segundos para o MCP Server iniciar...
    timeout /t 10 /nobreak >nul
)
echo.

:: ============================================================
:: PASSO 2 - Celery Background Worker (host, usa .venv local)
:: ============================================================
echo [ARES] [2/4] Iniciando Celery Worker em background...
if not exist "%SRA_REPO%\.venv\Scripts\celery.exe" (
    echo [ARES] [AVISO] Celery nao encontrado no .venv. Instalando automaticamente...
    call "%SRA_REPO%\.venv\Scripts\activate.bat" && pip install celery redis
    if errorlevel 1 (
        echo [ARES] [ERRO] Falha ao instalar Celery. Worker sera iniciado mesmo assim.
    )
)
start "SRA Celery Worker" cmd /k "cd /d \"%SRA_REPO%\" && call .venv\Scripts\activate.bat && celery -A src.worker.celery_app worker --loglevel=info --pool=solo"

:: ============================================================
:: PASSO 3 - Streamlit Web UI (host, usa .venv local)
:: ============================================================
echo [ARES] [3/4] Iniciando Web UI Streamlit (Porta 8501)...
start "SRA Streamlit Web UI" cmd /k "cd /d \"%SRA_REPO%\" && call .venv\Scripts\activate.bat && streamlit run ui/streamlit_app.py --server.port 8501"

:: ============================================================
:: PASSO 4 - Firecrawl (INICIAR MANUALMENTE QUANDO NECESSARIO)
:: ============================================================
echo [ARES] [4/4] Firecrawl New nao iniciado automaticamente (evita OOM do Docker Desktop).
echo [ARES] Para scraping avancado (JS/SPA/WAF bypassing), inicie separadamente:
echo [ARES]   ^> START_Firecrawl.bat
echo.

:: ============================================================
:: PASSO 5 - Abrir Navegadores & Health Check
:: ============================================================
echo [ARES] Aguardando 5 segundos para a subida das portas locais...
timeout /t 5 /nobreak >nul

echo [ARES] Abrindo a interface Streamlit no browser...
start "" "http://localhost:8501"

echo [ARES] Abrindo a documentacao OpenAPI (Swagger) no browser...
start "" "http://localhost:3458/docs"

:: ============================================================
:: INFORMACOES DE CONEXAO
:: ============================================================
echo.
echo [ARES] === INFORMACOES DE CONEXAO (SRA-V3.0 SUPREMO) ===
echo [ARES] MCP Server:         http://localhost:3458
echo [ARES] REST API (Swagger): http://localhost:3458/docs
echo [ARES] MCP Server (SSE):   http://localhost:3458/mcp/sse
echo [ARES] Prometheus Metrics: http://localhost:8001/metrics
echo [ARES] Redis Cache/Celery: redis://localhost:6379/0
echo [ARES] KuzuDB Graph DB:    Local (Embedded) - Leiden/Louvain e GraphRAG ativos
echo [ARES] Neo4j Graph DB:     Opcional via --profile neo4j (bolt://localhost:7687)
echo [ARES] ChromaDB Vector:    http://localhost:3024
echo [ARES] SearXNG Search:     http://localhost:8080
echo [ARES] Firecrawl API:      http://localhost:3022 (inicie com START_Firecrawl.bat)
echo [ARES] Tinyproxy Proxy:    http://localhost:3017
echo [ARES] Ollama Local LLM:   http://localhost:11434 (Qwen2.5-Coder:7b + Nomic-Embed-Text)
echo [ARES] CLI local:          python cli/main.py search "query" --mode guerrilha
echo.
if "!SRA_MODE!"=="docker" (
    echo [ARES] Backend: Docker (SRA + SearXNG + Redis + ChromaDB)
    echo [ARES] Scraping: usa fallback cascade (Firecrawl opcional via START_Firecrawl.bat)
) else (
    echo [ARES] Backend: HOST (modo fallback - sem Docker)
    echo [ARES] Para full-stack: inicie Docker Desktop e execute novamente.
)
echo.
echo [ARES] Inicializacao concluida com sucesso!
pause
