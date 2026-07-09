# MISSÃO PARTE2 — FASE 3: Segurança da API REST (Autenticação + CORS + Rate Limiting)

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 3 do plano derivado da `AUDITORIA_SRA_PARTE_2.md`.
> Pré-requisito: **Fase 1 concluída** (wiring test passando).
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — POR QUE ISSO É URGENTE

A API REST (`api/main.py`) está exposta com:
- **Zero autenticação** — qualquer pessoa que alcance o IP/porta pode disparar pesquisas
- **CORS aberto** — `allow_origins=["*"]` com comentário no próprio código dizendo que não deve ir para produção assim
- **Bind em `0.0.0.0`** — o Dockerfile e docker-compose expõem o serviço em todas as interfaces

Cada pesquisa consome LLM tokens (custo real), pode acionar scraping pago (Firecrawl/Spider) e executa código em sandbox Docker. Resolver isso antes de adicionar mais integrações pagas é crítico.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `security-hardening` | `.claude/skills/security-hardening/SKILL.md` | Para autenticação, CORS e rate limiting |
| `api-patterns` | `.claude/skills/api-patterns/SKILL.md` | Para o padrão de `Depends()` e estrutura do endpoint |
| `python-pro` | `.claude/skills/python-pro/SKILL.md` | Para código FastAPI + Pydantic Settings |

---

## 📋 TAREFAS

### TAREFA 3.1 — Adicionar API Key própria do SRA via `Depends()`

**Arquivo alvo:** `api/main.py` e `src/config.py`

**O que fazer:**

1. Adicionar `sra_api_key: str | None = None` em `src/config.py` (Pydantic BaseSettings).
2. Adicionar a variável `SRA_API_KEY=` em `.env.example` com comentário explicando que deve ser gerada aleatoriamente.
3. Em `api/main.py`, criar a dependência de verificação:

```python
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(
    api_key: str | None = Security(_api_key_header),
    config: Config = Depends(get_config),  # adapte conforme como Config é injetado hoje
) -> None:
    """
    Verifica a API Key do SRA.
    Se SRA_API_KEY não estiver configurada no .env, a autenticação é desabilitada
    (compatibilidade com uso local sem configuração).
    """
    if not config.sra_api_key:
        # Modo sem auth: apenas logar aviso uma vez no startup
        return
    if not api_key or api_key != config.sra_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key. Use header: X-API-Key: <your-key>",
            headers={"WWW-Authenticate": "ApiKey"},
        )
```

4. Aplicar `verify_api_key` como dependência nos **endpoints de pesquisa** (não precisa proteger `/health` e `/docs`):
```python
@app.post("/api/research", dependencies=[Depends(verify_api_key)])
async def research(...):
    ...
```

5. Adicionar log de aviso no startup quando `sra_api_key` não está configurado:
```python
@app.on_event("startup")
async def startup_event():
    config = get_config()
    if not config.sra_api_key:
        logger.warning(
            "SRA_API_KEY not configured. API is running without authentication. "
            "Set SRA_API_KEY in .env for production use."
        )
```

---

### TAREFA 3.2 — Restringir CORS via configuração

**Arquivo alvo:** `api/main.py`

**O que fazer:**

1. Adicionar `cors_allowed_origins: list[str] = ["*"]` em `src/config.py`.
2. Adicionar `CORS_ALLOWED_ORIGINS=*` em `.env.example` com comentário:
   ```
   # Para produção, restringir às origens reais: CORS_ALLOWED_ORIGINS=https://seu-app.com
   ```
3. Em `api/main.py`, substituir o `allow_origins=["*"]` hardcoded por:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

---

### TAREFA 3.3 — Adicionar rate limiting nos endpoints de pesquisa

**Arquivo alvo:** `api/main.py` e `pyproject.toml`

**O que fazer:**

1. Adicionar `slowapi` às dependências em `pyproject.toml`:
   ```toml
   "slowapi>=0.1.9",
   ```
2. Em `api/main.py`, configurar o rate limiter:
```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
```

3. Aplicar rate limit nos endpoints de pesquisa (custo alto):
```python
@app.post("/api/research")
@limiter.limit("10/minute")  # ajustar conforme necessidade
async def research(request: Request, ...):
    ...
```
> Adapte o limite conforme o custo esperado por request. 10/min por IP é um ponto de partida conservador.

---

### TAREFA 3.4 — Adicionar `pip-audit` ao CI

**Arquivo alvo:** `.github/workflows/ci.yml`

**O que fazer:**

1. Localizar a seção de jobs do CI.
2. Adicionar um step de auditoria de dependências:
```yaml
- name: Audit dependencies for vulnerabilities
  run: |
    pip install pip-audit
    pip-audit --requirement requirements.txt || pip-audit .
  continue-on-error: false  # falhar o CI em vulnerabilidades conhecidas
```
> Adapte o comando conforme o gerenciador de dependências do projeto (`pip-audit .` funciona com `pyproject.toml`).

---

### TAREFA 3.5 — Atualizar README com seção de segurança para produção

**Arquivo alvo:** `README.md`

Adicionar uma seção `## Segurança em Produção` com:
- Instruções para configurar `SRA_API_KEY`
- Instruções para configurar `CORS_ALLOWED_ORIGINS`
- Recomendação de usar proxy reverso (nginx/Caddy) para produção
- Aviso de que a configuração padrão (sem auth) é para desenvolvimento local

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 3

- [ ] `SRA_API_KEY` em `Config` e `.env.example`
- [ ] Endpoints de pesquisa retornam `401` quando `SRA_API_KEY` configurado e header ausente
- [ ] Endpoints retornam `200` quando `SRA_API_KEY` não configurado (backward compatibility)
- [ ] CORS lê `CORS_ALLOWED_ORIGINS` do `.env`
- [ ] Rate limit configurado e funcionando (testar com `curl` em loop)
- [ ] `pip-audit` adicionado ao `ci.yml`
- [ ] README atualizado com seção de segurança
- [ ] `python -m pytest tests/ --tb=short -q` → suíte completa sem novas falhas

---

## 🚫 FORA DO ESCOPO DESTA FASE

- `ResultID` canônico / `FeedbackRanker` → Fase 4
- `SanitizationStage.run()` → Fase 4
- `GenericAPISearcher` → Fase 6+
- Paridade CLI/API/UI → decisão de produto
