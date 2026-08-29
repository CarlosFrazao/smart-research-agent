#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# start-prod.sh — Inicia o Smart Research Agent em modo PRODUÇÃO
#
# Requisitos antes de rodar:
#   1. Copiar .env.example → .env e preencher TODAS as chaves (SRA_API_KEY,
#      REDIS_PASSWORD, GRAFANA_ADMIN_PASSWORD, CHROMA_AUTH_SECRET, etc.)
#   2. O script falha-fast (exit 1) se SRA_ENV != "production" ou se
#      SRA_API_KEY não estiver configurada.
#
# Uso:
#   ./start-prod.sh              # sobe a stack completa em produção
#   ./start-prod.sh --logs       # sobe e acompanha os logs em tempo real
#   ./start-prod.sh --down       # derruba a stack de produção
#   ./start-prod.sh --restart    # recomeça (down + up)
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

FOLLOW_LOGS=false
TEARDOWN=false
RESTART=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --logs)     FOLLOW_LOGS=true; shift ;;
    --down)     TEARDOWN=true; shift ;;
    --restart)  RESTART=true; shift ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//' ; exit 0 ;;
    *)          echo "Opção desconhecida: $1" >&2; exit 1 ;;
  esac
done

# Verifica Docker
if ! command -v docker &>/dev/null; then
  echo "❌ Docker não encontrado." >&2; exit 1
fi
if ! docker compose version &>/dev/null; then
  echo "❌ Docker Compose v2 não encontrado." >&2; exit 1
fi
if ! docker info &>/dev/null; then
  echo "❌ O daemon do Docker não está rodando." >&2; exit 1
fi

# Verifica .env
if [[ ! -f .env ]]; then
  echo "❌ .env não encontrado. Copie .env.example e configure para produção." >&2
  exit 1
fi

# Validações de produção
if ! grep -q '^SRA_ENV=production' .env; then
  echo "❌ SRA_ENV deve ser 'production'. Configure no .env e tente novamente." >&2
  exit 1
fi

if ! grep -q '^SRA_API_KEY=' .env || grep -q '^SRA_API_KEY=$' .env; then
  echo "❌ SRA_API_KEY não configurada. Configure no .env e tente novamente." >&2
  exit 1
fi

if ! grep -q '^REDIS_PASSWORD=' .env || grep -q '^REDIS_PASSWORD=$' .env; then
  echo "❌ REDIS_PASSWORD não configurada. Configure no .env e tente novamente." >&2
  exit 1
fi

if ! grep -q '^GRAFANA_ADMIN_PASSWORD=' .env || grep -q '^GRAFANA_ADMIN_PASSWORD=$' .env; then
  echo "❌ GRAFANA_ADMIN_PASSWORD não configurada. Configure no .env e tente novamente." >&2
  exit 1
fi

if $RESTART; then
  echo "🔄 Reiniciando stack de produção..."
  docker compose --profile prod down
  docker compose --profile prod up -d --build
elif $TEARDOWN; then
  echo "🛑 Derrubando a stack de produção..."
  docker compose --profile prod down
  exit 0
fi

echo "🚀 Subindo Smart Research Agent — modo PRODUÇÃO"
docker compose --profile prod up -d --build

echo ""
echo "✅ Stack no ar:"
echo "   • API SRA:               http://localhost:3458/docs"
echo "   • Health check:          http://localhost:3458/health"
echo "   • Prometheus:            http://localhost:9090"
echo "   • Grafana:               http://localhost:3000"
echo "   • SearXNG:               http://localhost:8080"
echo ""
echo "   Logs:                     docker compose --profile prod logs -f"
echo "   Derrubar:                 ./start-prod.sh --down"

if $FOLLOW_LOGS; then
  docker compose --profile prod logs -f
fi
