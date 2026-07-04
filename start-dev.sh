#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# start-dev.sh — Setup rápido do Smart Research Agent em modo dev mínimo
#
# Sobe apenas o essencial para desenvolver localmente: o SRA + SearXNG.
# ChromaDB, Neo4j, Redis e Firecrawl são OPCIONAIS (o código já faz fallback
# gracioso quando ausentes) e podem ser ligados sob demanda com:
#
#   ./start-dev.sh --with chromadb,redis
#   docker compose --profile full up -d      # stack completa
#
# Uso:
#   ./start-dev.sh              # sobe SRA + SearXNG
#   ./start-dev.sh --with X,Y   # sobe SRA + SearXNG + perfis extras X e Y
#   ./start-dev.sh --logs       # sobe e acompanha os logs
#   ./start-dev.sh --down       # derruba a stack dev
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PROFILES=("dev")
FOLLOW_LOGS=false
TEARDOWN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with)
      IFS=',' read -ra EXTRA <<< "$2"
      for p in "${EXTRA[@]}"; do PROFILES+=("$p"); done
      shift 2
      ;;
    --logs)
      FOLLOW_LOGS=true
      shift
      ;;
    --down)
      TEARDOWN=true
      shift
      ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Opção desconhecida: $1" >&2
      exit 1
      ;;
  esac
done

# Verifica se o Docker está instalado e o daemon está de pé
if ! command -v docker &>/dev/null; then
  echo "❌ Docker não encontrado. Instale o Docker antes de continuar." >&2
  exit 1
fi
if ! docker compose version &>/dev/null; then
  echo "❌ Docker Compose v2 não encontrado (comando 'docker compose')." >&2
  exit 1
fi
if ! docker info &>/dev/null; then
  echo "❌ O daemon do Docker não está rodando. Inicie o Docker e tente novamente." >&2
  exit 1
fi

# Monta a lista de flags --profile
PROFILE_FLAGS=()
for p in "${PROFILES[@]}"; do
  PROFILE_FLAGS+=(--profile "$p")
done

if $TEARDOWN; then
  echo "🛑 Derrubando a stack dev..."
  docker compose "${PROFILE_FLAGS[@]}" down
  exit 0
fi

# Garante que o .env existe
if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    echo "⚠️  .env não encontrado — copiando de .env.example."
    echo "   Edite o .env com suas chaves de API antes de rodar pesquisas reais."
    cp .env.example .env
  else
    echo "❌ Nem .env nem .env.example foram encontrados." >&2
    exit 1
  fi
fi

echo "🚀 Subindo Smart Research Agent — perfis: ${PROFILES[*]}"
docker compose "${PROFILE_FLAGS[@]}" up -d --build

echo ""
echo "✅ Stack no ar:"
echo "   • API SRA (Swagger):  http://localhost:3458/docs"
echo "   • Health check:       http://localhost:3458/health"
if [[ " ${PROFILES[*]} " == *" dev "* || " ${PROFILES[*]} " == *" full "* ]]; then
  echo "   • SearXNG:            http://localhost:8080"
fi
if [[ " ${PROFILES[*]} " == *" chromadb "* || " ${PROFILES[*]} " == *" full "* ]]; then
  echo "   • ChromaDB:           http://localhost:3024"
fi
if [[ " ${PROFILES[*]} " == *" neo4j "* || " ${PROFILES[*]} " == *" full "* ]]; then
  echo "   • Neo4j Browser:      http://localhost:7474"
fi
echo ""
echo "   Para derrubar:        ./start-dev.sh --down"
echo "   Para ver logs:        docker compose ${PROFILE_FLAGS[*]} logs -f"

if $FOLLOW_LOGS; then
  docker compose "${PROFILE_FLAGS[@]}" logs -f
fi
