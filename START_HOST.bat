@echo off
title SRA Dashboard — Host Mode (sem Docker)
color 0A

echo ========================================
echo  Smart Research Agent — HOST MODE
echo  ARES-V4.3 ^| Porta: 3458
echo ========================================
echo.

:: Verificar Python
python --version >nul 2>&1 || (
    echo [ERRO] Python nao encontrado. Instale e adicione ao PATH.
    pause
    exit /b 1
)

:: Navegar para a pasta do projeto
cd /d "E:\Meus LLMs\smart-research-agent"

:: Ativar ambiente virtual se existir
if exist ".venv\Scripts\activate.bat" (
    echo [OK] Ativando ambiente virtual .venv...
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    echo [OK] Ativando ambiente virtual venv...
    call venv\Scripts\activate.bat
) else (
    echo [AVISO] Nenhum venv encontrado, usando Python global.
)

:: Configurar Host Mode
set HOST_MODE=true
set JINA_READER_BASE_URL=https://r.jina.ai/

:: Verificar se Firecrawl Docker esta disponivel
curl -s --max-time 2 http://localhost:3002/health >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [OK] Firecrawl Docker detectado na porta 3002 - modo hibrido ativo.
    set FIRECRAWL_BASE_URL=http://localhost:3002
) else (
    echo [AVISO] Firecrawl nao detectado - usando Jina Reader como fallback.
    set FIRECRAWL_BASE_URL=
)

echo.
echo [OK] Iniciando SRA Dashboard na porta 3458...
echo [OK] Acesse: http://localhost:3458
echo.
echo Pressione CTRL+C para encerrar.
echo ========================================
echo.

uvicorn src.mcp_server:app --host 0.0.0.0 --port 3458 --reload --log-level info

pause
