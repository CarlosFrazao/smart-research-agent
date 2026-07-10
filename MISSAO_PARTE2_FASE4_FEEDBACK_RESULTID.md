# MISSÃO PARTE2 — FASE 4: Sistema de Feedback (ResultID Canônico + FeedbackRanker)

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 4 do plano derivado da `AUDITORIA_SRA_PARTE_2.md`.
> Pré-requisito: **Fase 1 concluída** (wiring test passando).
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — POR QUE ISSO É CRÍTICO

O sistema de feedback está **quebrado em dois pontos simultâneos**:

1. **IDs incompatíveis:** `POST /feedback` gera `result_id = sha1(query)` enquanto `FeedbackRanker._result_id()` gera `sha1(entity:title)`. Os dois hashes **nunca coincidem** — o ranker sempre recebe score `0.0` para todos os resultados, tornando o ajuste por feedback funcionalmente inerte mesmo com dados acumulados.

2. **FeedbackRanker nunca instanciado:** `FeedbackRanker` existe, tem testes, mas não é instanciado em nenhum código de produção (`src/`, `api/`).

**A Fase 4 do primeiro plano (personalização por fonte) foi construída sobre essa fundação quebrada.** Antes de avançar com qualquer personalização nova, a cadeia `resultado → result_id → FeedbackStore → FeedbackRanker → score ajustado` precisa funcionar de ponta a ponta.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `.claude/skills/python-pro/SKILL.md` | Para implementação do ResultID e integração |
| `test-driven-development` | `.claude/skills/test-driven-development/SKILL.md` | Para o teste de integração do ciclo completo |
| `clean-code` | `.claude/skills/clean-code/SKILL.md` | Para refatoração dos pontos onde os IDs são gerados hoje |

---

## 📋 TAREFAS (em ordem obrigatória — há dependência)

### TAREFA 4.1 — Criar `ResultID` canônico em `src/models.py` ou módulo equivalente

**Contexto:** Hoje cada módulo que precisa de um "ID de resultado" inventa sua própria função de hash com entradas diferentes. A solução é um `ResultID` gerado uma única vez, na fonte (no momento em que o resultado bruto chega do searcher), e propagado por todo o pipeline.

**O que fazer:**

1. Localizar onde os modelos de resultado são definidos (provavelmente `src/models.py` ou similar — abra o arquivo para confirmar a estrutura do objeto `ResearchResult` / `SearchResult`).

2. Criar uma função utilitária `generate_result_id`:
```python
import hashlib

def generate_result_id(source_name: str, url: str) -> str:
    """
    Gera um ID canônico e determinístico para um resultado de pesquisa.

    O ID é baseado em (source_name, url) — dois resultados do mesmo URL
    e mesma fonte sempre produzem o mesmo ID, independente de quando foram buscados.

    Use este ID em todo o pipeline: FeedbackStore, FeedbackRanker, HITL, UI.
    """
    raw = f"{source_name}:{url}".lower().strip()
    return hashlib.sha1(raw.encode()).hexdigest()[:12]
```

3. Garantir que o modelo `SearchResult` (ou equivalente) tenha um campo `result_id: str` que seja preenchido no momento da criação do resultado em `search_stage.py`.

4. Em `src/pipeline/stages/search_stage.py`, no ponto onde os resultados brutos são coletados de cada searcher, preencher o `result_id`:
```python
from src.models import generate_result_id  # ajuste o caminho conforme necessário

# dentro do loop de processamento de resultados:
result.result_id = generate_result_id(
    source_name=source_name,
    url=result.url or result.link or "",  # ajuste o campo conforme o modelo real
)
```

---

### TAREFA 4.2 — Corrigir `POST /feedback` para aceitar `result_id` por resultado individual

**Arquivo alvo:** `src/mcp_server.py` (onde o endpoint `/feedback` está montado) e/ou `api/main.py`

**Contexto:** O endpoint atual só aceita `{ query: str, signal: str }` e deriva um ID da query inteira. O novo design deve aceitar o `result_id` do resultado específico que o usuário está avaliando.

**O que fazer:**

1. Abrir o arquivo onde `POST /feedback` está definido (confirmar se é em `src/mcp_server.py`, `api/main.py` ou ambos).

2. Atualizar o schema da request para incluir `result_id` (mantendo `query` como campo opcional para backward compatibility):
```python
class FeedbackRequest(BaseModel):
    query: str
    signal: str  # "useful" | "not_useful"
    result_id: str | None = None    # ID do resultado específico (novo campo)
    source_name: str | None = None  # nome da fonte do resultado (novo campo)
```

3. Atualizar a lógica de geração do `result_id` no handler:
```python
# Se result_id foi enviado pelo cliente (novo comportamento), usar diretamente
# Se não foi enviado (comportamento legado), manter compatibilidade
effective_result_id = payload.result_id or hashlib.sha1(payload.query.lower().encode()).hexdigest()[:12]

await feedback_store.record(
    user_id=...,
    query=payload.query,
    result_id=effective_result_id,
    signal=payload.signal,
    source_name=payload.source_name,  # novo campo
)
```

---

### TAREFA 4.3 — Adicionar `source_name` ao `FeedbackStore.record()`

**Arquivo alvo:** `src/feedback_store.py`

**Contexto:** `FeedbackStore.record()` não tem parâmetro `source_name`. Sem isso, a Fase 4 do plano original (reponderar fontes por usuário) não tem dados para agregar.

**O que fazer:**

1. Abrir `src/feedback_store.py` e localizar o método `record()`.
2. Adicionar `source_name: str | None = None` como parâmetro (com default None para backward compatibility).
3. Incluir `source_name` no registro JSONL gravado em disco.
4. Verificar se `get_source_weights()` (usado na Fase 4 do plano original) já usa esse campo ou se precisa ser atualizado para lê-lo.

---

### TAREFA 4.4 — Conectar `FeedbackRanker` ao pipeline de produção

**Arquivo alvo:** `src/pipeline/stages/rank_stage.py` ou `score_stage.py` (confirmar qual é o stage correto)

**Contexto:** `FeedbackRanker` nunca é instanciado em código de produção — só existe em testes. É mais uma instância do padrão sistêmico de "implementado mas nunca conectado".

**O que fazer:**

1. Abrir `src/ranking/feedback_ranker.py` (ou onde estiver) e ler a assinatura do método `apply()`.
2. No stage de ranking (`rank_stage.py`), instanciar e chamar `FeedbackRanker.apply()` após o ranking inicial, passando os `result_id`s dos resultados já gerados:
```python
from src.ranking.feedback_ranker import FeedbackRanker

# No método run() do stage:
if self.feedback_store:
    ranker = FeedbackRanker(self.feedback_store)
    context.ranked_results = await ranker.apply(
        results=context.ranked_results,
        user_id=context.user_id,
        query=context.query,
    )
```
> Adapte conforme a assinatura real do `FeedbackRanker.apply()`.

---

### TAREFA 4.5 — Escrever o teste de integração do ciclo completo de feedback

**Arquivo alvo:** `tests/test_feedback_cycle_integration.py` (arquivo NOVO)

**O que fazer:**
```python
"""
Teste de integração do ciclo completo de feedback.

Valida que a cadeia: SearchStage gera result_id → FeedbackStore.record() →
FeedbackRanker.apply() → combined_score alterado funciona de ponta a ponta.

Este teste teria pego o bug dos IDs incompatíveis imediatamente.
"""
import pytest
from unittest.mock import MagicMock

from src.models import generate_result_id
from src.feedback_store import FeedbackStore
from src.ranking.feedback_ranker import FeedbackRanker  # ajuste o caminho


class TestFeedbackCycleIntegration:

    def test_result_id_is_deterministic(self):
        """O mesmo (source, url) sempre gera o mesmo result_id."""
        id1 = generate_result_id("github", "https://github.com/user/repo")
        id2 = generate_result_id("github", "https://github.com/user/repo")
        assert id1 == id2

    def test_result_id_differs_by_source_and_url(self):
        """IDs diferentes para URLs e fontes diferentes."""
        id_github = generate_result_id("github", "https://github.com/user/repo")
        id_arxiv = generate_result_id("arxiv", "https://arxiv.org/abs/1234")
        id_diff_url = generate_result_id("github", "https://github.com/user/other")
        assert id_github != id_arxiv
        assert id_github != id_diff_url

    @pytest.mark.asyncio
    async def test_feedback_affects_ranker_score(self, tmp_path):
        """
        CRÍTICO: Após record() com signal='useful', FeedbackRanker.apply()
        deve produzir um combined_score diferente para aquele resultado.
        """
        store = FeedbackStore(storage_path=tmp_path / "feedback.jsonl")
        ranker = FeedbackRanker(store)

        result_id = generate_result_id("github", "https://github.com/user/repo")

        # Criar um resultado mock com o result_id canônico
        mock_result = MagicMock()
        mock_result.result_id = result_id
        mock_result.combined_score = 0.5

        # Registrar feedback positivo
        await store.record(
            user_id="test-user",
            query="python async patterns",
            result_id=result_id,
            signal="useful",
            source_name="github",
        )

        # Verificar que o ranker ajusta o score
        results_before = [mock_result]
        results_after = await ranker.apply(
            results=[mock_result],
            user_id="test-user",
            query="python async patterns",
        )

        # O score deve ter mudado positivamente
        assert results_after[0].combined_score != 0.5, (
            "FeedbackRanker não alterou o score — verifique se result_id está sendo "
            "comparado corretamente entre FeedbackStore e FeedbackRanker"
        )
```

---

### TAREFA 4.7 — Conectar `ResearchAuditor` ao `report_stage.py` ⭐ (achado §14.1 — maior impacto percebido)

**Arquivo alvo:** `src/pipeline/stages/report_stage.py` (e eventualmente `src/orchestrator.py`)

**Contexto:** `src/research_auditor.py` implementa um **loop completo de verificação de claims/fact-checking**:
- Extrai claims do relatório final via LLM
- Valida cada uma contra as fontes já coletadas (`ConfidenceScorerV2`)
- Detecta gaps (claims não verificadas ou de fonte única)
- Relança buscas focadas nos gaps (com teto de iterações e orçamento via `token_economy.Budget`)
- Devolve o relatório enriquecido com notas de auditoria

O `ResearchAuditor` **está instanciado** em `src/pipeline/stage_factory.py:175` como `orchestrator.auditor = ResearchAuditor(...)`. O próprio docstring da classe documenta como deve ser chamado:
```python
# docstring de research_auditor.py:
# Integração com o Orchestrator: auditor = ResearchAuditor(...); audit = await auditor.audit(...)
```

Mas `orchestrator.auditor.audit(...)` **nunca é chamado** em nenhum stage — é literalmente o módulo mais completo e documentado de todos os achados de "módulo órfão" desta auditoria.

**O que fazer:**

1. Abrir `src/research_auditor.py` e ler a assinatura completa de `audit()`.
2. Abrir `src/pipeline/stages/report_stage.py` e localizar onde o relatório final é gerado.
3. Chamar `auditor.audit()` **após** a geração do relatório e **antes** de retornar ao usuário:

```python
# Em report_stage.py, no método run():
# Gera o relatório (como já faz hoje)
report_text = await self.report_generator.generate(context)

# NOVO: Auditoria de claims (§14.1)
if hasattr(context, "orchestrator") and hasattr(context.orchestrator, "auditor"):
    try:
        audit_result = await context.orchestrator.auditor.audit(
            report_text=report_text,
            existing_results=context.ranked_results,
        )
        # Adicionar notas de auditoria ao relatório ou ao contexto
        context.audit_result = audit_result
        # Se o auditor enriqueceu o report_text, usar o enriquecido:
        if hasattr(audit_result, "enriched_report"):
            report_text = audit_result.enriched_report
    except Exception as e:
        logger.warning("ResearchAuditor failed (non-fatal): %s", e)
        # Continuar com relatório sem auditoria — não travar o pipeline
```
> Adapte conforme a assinatura real de `auditor.audit()` — leia o docstring completo antes de escrever.

4. Garantir que o budget de auditoria seja configurável (o mecanismo já existe em `token_economy.Budget` — só verifique como o auditor o usa internamente).

**Validação:**
```bash
python -m pytest tests/ -k "auditor or research_auditor" -v  # testes existentes devem passar
python -m py_compile src/pipeline/stages/report_stage.py     # sem erros de sintaxe
```

---


### TAREFA 4.6 — Implementar `SanitizationStage.run()` (dívida técnica crítica)

**Arquivo alvo:** `src/pipeline/stages/__init__.py` (onde `SanitizationStage` está definida como stub)

**Contexto:** `SanitizationStage` tem `async def run(self, context): pass` — literalmente não faz nada. Mas está em `DEFAULT_STAGE_NAMES`, ou seja, **toda pesquisa executa uma etapa de sanitização que não faz nada**. O comentário no arquivo diz "Stub de compatibilidade temporária para o pipeline de 9 estágios legado".

**O que fazer:**

1. Abrir `src/pipeline/stages/__init__.py` e localizar `SanitizationStage`.
2. Abrir `src/services/llm_sanitizer.py` (ou onde `LLMSanitizer` está definido) para entender sua API.
3. Implementar `SanitizationStage.run()` chamando de fato o `LLMSanitizer`:
```python
async def run(self, context: PipelineContext) -> None:
    """Sanitiza os resultados de busca usando LLMSanitizer."""
    if not context.search_results:
        return

    sanitizer = LLMSanitizer(config=self.config)  # ajuste conforme assinatura real
    try:
        context.search_results = await sanitizer.sanitize(context.search_results)
    except Exception as e:
        logger.warning("SanitizationStage failed (non-fatal): %s", e)
        # Continuar com results não sanitizados em vez de travar o pipeline
```

**Validação:**
```bash
python -m pytest tests/ -k "sanitiz" -v  # testes existentes de sanitização
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 4

- [ ] `generate_result_id(source, url)` existe em `src/models.py` (ou equivalente)
- [ ] `search_stage.py` preenche `result.result_id` usando `generate_result_id`
- [ ] `POST /feedback` aceita `result_id` e `source_name` nos campos da request
- [ ] `FeedbackStore.record()` persiste `source_name` no JSONL
- [ ] `FeedbackRanker` instanciado e chamado no stage de ranking de produção
- [ ] `python -m pytest tests/test_feedback_cycle_integration.py -v` → todos passam
- [ ] `python -m pytest tests/ -k "feedback or ranker" -v` → sem regressões
- [ ] `SanitizationStage.run()` implementado e não mais um `pass`
- [ ] `orchestrator.auditor.audit()` chamado em `report_stage.py` após geração do relatório ⭐ (§14.1)
- [ ] `python -m pytest tests/ -k "auditor or research_auditor" -v` → sem regressões
- [ ] `python -m pytest tests/ --tb=short -q` → suíte completa sem novas falhas

---

## 🚫 FORA DO ESCOPO DESTA FASE

- `GenericAPISearcher` e `GenericWebsiteSearcher` → Fase 6
- Monitoramento contínuo via `scheduler.py` → Fase 6
- Novas fontes verticais (OpenLibrary, CORE, OSM, etc.) → Fase 6
- Paridade completa de UI Streamlit com API REST → decisão de produto
