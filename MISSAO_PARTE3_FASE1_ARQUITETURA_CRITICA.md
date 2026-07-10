# MISSÃO PARTE3 — FASE 1: Correções Críticas de Arquitetura

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 1 do plano derivado de `PLANO_SRA_PARTE_3.md`.
> Pré-requisito: **Auditoria Parte 2 completa** (commits `ab9ca71` → `7479b36`).
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — POR QUE ESSA FASE É CRÍTICA E VEM PRIMEIRO

Existem três subsistemas completos **órfãos** e uma **divergência arquitetural grave** entre dois servidores FastAPI. Construir features novas sobre essa base é como adicionar andares num prédio com a fundação rachada. Esta fase corrige os achados de wiring de maior impacto antes de qualquer desenvolvimento novo.

### Achados a corrigir (do PLANO_SRA_PARTE_3.md §14 e §15):

| Achado | Severidade | Esforço estimado |
|--------|-----------|------------------|
| `enable_auditor` flag nunca lida (§14.1) | Crítica | Poucas linhas |
| `ReActOrchestrator` inalcançável (§14.2) | Crítica | Moderado |
| Modo `"debate"` ausente do UI (§14.3) | Baixa | Trivial |
| `src/search/common/` — diretório morto (§15.1) | Média | Trivial |
| Dois servidores FastAPI divergentes (§15.2) | **Crítica** | Maior complexidade |

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `systematic-debugging` | `E:\Meus LLMs\.claude\skills\systematic-debugging\SKILL.md` | Diagnóstico dos subsistemas órfãos antes de qualquer mudança |
| `python-pro` | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | Todo código Python novo ou modificado |
| `api-patterns` | `E:\Meus LLMs\.claude\skills\api-patterns\SKILL.md` | Unificação dos servidores FastAPI |
| `security-hardening` | `E:\Meus LLMs\.claude\skills\security-hardening\SKILL.md` | Auth/CORS/rate-limit no servidor de produção correto |

---

## 📋 TAREFAS (em ordem obrigatória)

### TAREFA 1.1 — Conectar `enable_auditor` ao `ResearchAuditor` via `operation_modes.py`

**Contexto (§14.1):** O modo `"cirurgia"` (padrão do sistema) anuncia "auditoria cruzada e verificação de cada claim" e define `enable_auditor=True` — mas essa flag nunca é lida em nenhum `if` real. O elo que falta é de **poucas linhas**.

**O que fazer:**

1. Abrir `src/operation_modes.py` e confirmar a estrutura de `OperationConfig.enable_auditor`.
2. Abrir `src/pipeline/stages/report_stage.py` (ou `src/orchestrator.py`) e localizar onde o relatório final é gerado.
3. Adicionar a verificação da flag **antes de chamar o auditor** (que já foi instanciado em `stage_factory.py:175` na Fase 4 da Parte 2):

```python
# Em report_stage.py ou orchestrator.py, após gerar o relatório:
operation_mode = getattr(context, "operation_mode", None) or getattr(self, "operation_mode", None)
if operation_mode and getattr(operation_mode, "enable_auditor", False):
    auditor = getattr(context, "auditor", None) or getattr(self, "auditor", None)
    if auditor:
        try:
            audit_result = await auditor.audit(
                report_text=report_text,
                existing_results=context.ranked_results,
            )
            if hasattr(audit_result, "enriched_report"):
                report_text = audit_result.enriched_report
            context.extra["audit_result"] = audit_result
        except Exception as exc:
            logger.warning("ResearchAuditor failed (non-fatal): %s", exc)
```

> Adapte o código ao contexto real do arquivo — confirme as assinaturas antes de escrever.

**Validação:**
```bash
python -m pytest tests/ -k "auditor" -v
python -m py_compile src/pipeline/stages/report_stage.py
```

---

### TAREFA 1.2 — Conectar `ReActOrchestrator` nos pontos de entrada reais

**Contexto (§14.2):** `ReActOrchestrator` existe, tem testes, tem docstring de integração — mas todos os 4 entry points sempre instanciam `Orchestrator` diretamente, tornando `enable_dynamic_loop=True` inerte.

**O que fazer:**

1. Abrir `src/react_orchestrator.py` e ler a assinatura do construtor e do método principal.
2. Em cada um dos 4 pontos de entrada, substituir a instanciação direta por:

```python
# ANTES (nos 4 pontos: api/main.py x2, cli/main.py, src/mcp_server.py):
orchestrator = Orchestrator(config)

# DEPOIS:
from src.react_orchestrator import ReActOrchestrator

if getattr(config, "enable_dynamic_loop", False):
    orchestrator = ReActOrchestrator(config)
    logger.info("Usando ReActOrchestrator (loop dinâmico ativo)")
else:
    orchestrator = Orchestrator(config)
```

3. Confirmar que `EXPERIMENTAL_MODULES.md` (criado na Fase 5 da Parte 2) está atualizado para refletir que o módulo agora está conectado.

**Validação:**
```bash
python -m pytest tests/test_react_orchestrator.py -v
python -m py_compile api/main.py cli/main.py src/mcp_server.py
```

---

### TAREFA 1.3 — Adicionar modo `"debate"` ao seletor de modo na UI

**Contexto (§14.3):** `DebateOrchestrator` funciona perfeitamente, mas o `st.selectbox` de `ui/streamlit_app.py` não lista `"debate"`, então usuários da UI nunca conseguem ativá-lo.

**O que fazer:**

1. Abrir `ui/streamlit_app.py` e localizar o `st.selectbox` de seleção de modo.
2. Adicionar `"debate"` à lista de opções, com tooltip explicativo:

```python
# Localizar o selectbox de modo e adicionar "debate":
modo = st.selectbox(
    "Modo de operação",
    options=["cirurgia", "guerrilha", "radar", "arqueologia", "concorrencia", "black_ops", "debate"],
    # ... demais parâmetros existentes ...
)
```

3. Verificar se há algum `help=` ou texto descritivo por modo — se sim, adicionar descrição para `"debate"` (ex: "Motor de debate multi-agente: hipóteses opostas + juiz LLM").

**Validação:**
```bash
python -m py_compile ui/streamlit_app.py
```

---

### TAREFA 1.4 — Remover `src/search/common/` (diretório de código morto)

**Contexto (§15.1):** 6 arquivos / 495 linhas — versão antiga e superada da infraestrutura de HTTP/cache/circuit-breaker, hoje em `src/utils/` e `src/cache/`. Nenhum arquivo fora do próprio diretório o importa — nem testes.

**O que fazer:**

1. **ANTES de deletar**, confirmar com grep que não há uso dinâmico/reflection:
```bash
grep -rn "search.common\|search\/common" src/ api/ cli/ tests/ --include="*.py"
```

2. Se o grep retornar **zero** (esperado), remover o diretório:
```bash
# Windows PowerShell:
Remove-Item -Recurse -Force "src\search\common"
# Git:
git rm -r src/search/common/
```

3. Rodar a suíte de testes para confirmar zero regressões.

**Validação:**
```bash
python -m pytest tests/ --tb=short -q
grep -rn "search.common" src/ api/ cli/ tests/ --include="*.py"  # deve retornar vazio
```

---

### TAREFA 1.5 — Unificar os dois servidores FastAPI divergentes

**Contexto (§15.2):** `api/main.py` e `src/mcp_server.py` são dois `FastAPI()` independentes com endpoints diferentes. O README documenta `api/main.py`, o Dockerfile sobe `src/mcp_server.py`. **O servidor oficial é `src/mcp_server.py`** (é o que roda em produção via Docker e é o que expõe as 15 tools MCP).

**O que fazer:**

1. Abrir ambos os arquivos e mapear os endpoints exclusivos de `api/main.py` que NÃO existem em `src/mcp_server.py`:
   - `/api/research/async` (polling assíncrono por `task_id`)
   - `/api/research/{task_id}` (consulta de resultado por ID)
   - Contrato Pydantic de `ResearchRequest` (vs `dict` livre no mcp_server)

2. **Absorver como router** os endpoints exclusivos de `api/main.py` em `src/mcp_server.py`:
```python
# Em src/mcp_server.py:
from api.main import router as rest_router  # se api/main.py for refatorada para usar APIRouter
app.include_router(rest_router, prefix="/api/v2")  # prefixo novo evita conflito
```
> Alternativamente, se `api/main.py` usa `app` direto (não `APIRouter`), refatorar para `APIRouter` primeiro.

3. **Corrigir o bug crítico de erro silencioso** em `src/mcp_server.py`:
```python
# ANTES (retorna HTTP 200 mesmo quando falha):
except Exception as e:
    return {"error": str(e)}

# DEPOIS:
except Exception as e:
    logger.exception("Research pipeline error")
    raise HTTPException(status_code=500, detail=str(e))
```

4. **Aplicar auth/CORS/rate-limit** (já implementados na Fase 3 da Parte 2 em `api/main.py`) agora em `src/mcp_server.py` também — este é o servidor que de fato roda em produção.

5. **Atualizar o README** para documentar `src/mcp_server.py` como o servidor oficial:
```bash
uvicorn src.mcp_server:app --port 3458 --reload
```

6. **Documentar `api/main.py`** como legado/alternativo ou removê-lo (decisão: marcar com aviso no topo do arquivo e adicionar ao `EXPERIMENTAL_MODULES.md`).

**Validação:**
```bash
python -m py_compile src/mcp_server.py
python -m pytest tests/ -k "api or server or mcp" -v
# Smoke test manual: curl http://localhost:3458/health
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 1

- [ ] `enable_auditor=True` → `auditor.audit()` chamado de verdade no modo `"cirurgia"`
- [ ] `enable_dynamic_loop=True` → `ReActOrchestrator` instanciado nos 4 entry points
- [ ] Modo `"debate"` visível no `st.selectbox` da UI
- [ ] `src/search/common/` removido, grep confirma zero referências restantes
- [ ] `src/mcp_server.py` é o servidor oficial, com auth/CORS/rate-limit aplicados
- [ ] Erro de pesquisa em `src/mcp_server.py` retorna HTTP 500, não HTTP 200 com `{"error":...}`
- [ ] README atualizado para `uvicorn src.mcp_server:app`
- [ ] `python -m pytest tests/ --tb=short -q` → zero novas regressões
- [ ] Commit com todos os arquivos desta fase

---

## 🚫 FORA DO ESCOPO DESTA FASE

- `GenericAPISearcher` e novas fontes → Fase 3
- Clustering de resultados → Fase 4
- UI de allowlist/denylist → Fase 5
- `GenericWebsiteSearcher` → decisão de produto pendente, não implementar agora
