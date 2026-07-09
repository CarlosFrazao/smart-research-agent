# MISSÃO CLAUDE — Fase 4: Personalização por Perfil e Feedback de Fontes

**Projeto:** `E:\Meus LLMs\smart-research-agent`
**Pré-requisito:** Fases 0+1+2+3A+3B+3CDE já concluídas e em `main`. Execute `git pull origin main` antes de começar.

---

## CONTEXTO

O SRA já possui `feedback_store.py` e `feedback_ranker.py` que registram e aplicam feedback do usuário em resultados individuais. Mas hoje o feedback é armazenado por **resultado**, não por **fonte**. Esta fase adiciona a dimensão "qual fonte foi mais útil para este usuário" — gerando um sistema de personalização que reapondera as fontes dinamicamente conforme o histórico de cada usuário.

---

## LEIA ANTES DE TUDO

1. `E:\Meus LLMs\Conversa\PLANO_SRA_BUSCA_UNIVERSAL.md` — Seção "Fase 4"
2. `E:\Meus LLMs\CLAUDE.md` — Governança, skills e protocolo de boot
3. Leia os arquivos existentes ANTES de modificar:
   - `src/feedback/feedback_store.py`
   - `src/feedback/feedback_ranker.py`
   - `src/source_planner.py`

---

## SKILLS OBRIGATÓRIAS

Carregue ANTES de escrever qualquer código:
- `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md`
- `E:\Meus LLMs\.claude\skills\python-patterns\SKILL.md`
- `E:\Meus LLMs\.claude\skills\clean-code\SKILL.md`

---

## TAREFA 1 — Estender `FeedbackStore` com rastreio de fontes
**Arquivo:** `src/feedback/feedback_store.py`

Adicione um novo método ao `FeedbackStore` (sem quebrar nenhuma interface existente):

```python
def record_source_feedback(
    self,
    user_id: str,
    source_name: str,
    query_domain: str,
    was_useful: bool,
    result_score: float = 0.0,
) -> None:
    """Registra qual fonte gerou resultado aproveitado/ignorado pelo usuário."""
    # Gravar em uma tabela/estrutura separada de feedback_sources
    # Schema: {user_id, source_name, domain, was_useful, score, timestamp}
    ...

def get_source_weights(
    self,
    user_id: str,
    domain: str,
    available_sources: list[str],
) -> dict[str, float]:
    """Retorna pesos de confiança por fonte para um usuário/domínio.
    
    Retorna dict {source_name: weight} onde weight ∈ [0.0, 2.0].
    Weight > 1.0 = fonte historicamente útil para este usuário.
    Weight < 1.0 = fonte historicamente ignorada.
    Weight = 1.0 = sem histórico (neutro).
    """
    ...
```

**Regras de implementação:**
- Cold start: sem histórico → todas as fontes retornam weight=1.0
- Volume mínimo: só ajusta pesos após ≥5 feedbacks por fonte/usuário (evita overfitting)
- Peso máximo: 2.0 (não deixa nenhuma fonte dominar completamente)
- Peso mínimo: 0.2 (nenhuma fonte é completamente descartada)
- Fórmula simples: `weight = 1.0 + (useful_ratio - 0.5) * 2` onde `useful_ratio = approved / total`
- Persistência: use a mesma infraestrutura de storage já existente no `FeedbackStore`

---

## TAREFA 2 — Conectar `FeedbackStore` ao `record_feedback` MCP tool
**Arquivo:** `src/mcp/tools/feedback_tool.py` (ou onde a tool MCP de feedback está implementada)

Localize a tool `record_feedback` e adicione o rastreio de fonte:

```python
# Após registrar o feedback do resultado:
if feedback_store and result.get("source_name"):
    feedback_store.record_source_feedback(
        user_id=user_id or "default",
        source_name=result["source_name"],
        query_domain=context.get("domain", "general"),
        was_useful=rating > 3,
        result_score=result.get("score", 0.0),
    )
```

---

## TAREFA 3 — Reaponderar fontes no `SourcePlanner`
**Arquivo:** `src/source_planner.py`

No método `SourcePlanner.plan()`, após determinar `primary` e `secondary`, aplique os pesos:

```python
def _apply_user_weights(
    self,
    sources: list[str],
    user_id: str,
    domain: str,
) -> list[str]:
    """Reordena fontes conforme peso histórico do usuário.
    
    Fontes com maior peso ficam no início da lista (maior prioridade).
    """
    if not self.feedback_store or not user_id:
        return sources  # sem personalização → ordem padrão
    
    weights = self.feedback_store.get_source_weights(user_id, domain, sources)
    return sorted(sources, key=lambda s: weights.get(s, 1.0), reverse=True)
```

**Integração:** Chamar `_apply_user_weights()` no final de `plan()` se `user_id` for passado no contexto.

---

## TAREFA 4 — Política de privacidade e governança (documentação)
**Arquivo:** `docs/PRIVACY_FEEDBACK.md`

Crie o arquivo com o seguinte conteúdo:

```markdown
# Política de Privacidade — Sistema de Feedback por Fonte

## O que é armazenado

O SRA armazena, por usuário, quais fontes de busca geraram resultados
aproveitados ou ignorados. Os dados armazenados são:

- `user_id` — identificador anônimo da sessão (não vinculado a dados pessoais)
- `source_name` — nome da fonte (ex: "github", "wikipedia")
- `domain` — categoria da query (ex: "dev_tools", "universal")
- `was_useful` — boolean (aprovado/ignorado)
- `timestamp` — data/hora do feedback

## O que NÃO é armazenado

- Conteúdo da query
- Conteúdo dos resultados
- IP, email ou qualquer dado identificador pessoal

## Como resetar

Para resetar seu perfil de preferências de fonte, use o endpoint:
`DELETE /api/feedback/sources/{user_id}`
ou chame `feedback_store.clear_source_feedback(user_id)`.

## Volume mínimo

Pesos de fontes só passam a influenciar o resultado após 5+ feedbacks
por fonte/usuário, para evitar viés por amostra pequena.
```

---

## TAREFA 5 — Testes unitários
**Arquivo:** `tests/test_source_personalization.py`

```python
def test_cold_start_returns_equal_weights():
    """Usuário sem histórico → todas as fontes têm peso 1.0."""
    ...

def test_useful_source_gets_higher_weight():
    """Fonte com 80% de aprovação → weight > 1.0."""
    ...

def test_ignored_source_gets_lower_weight():
    """Fonte com 20% de aprovação → weight < 1.0."""
    ...

def test_minimum_volume_threshold():
    """Com menos de 5 feedbacks, peso permanece 1.0."""
    ...

def test_weight_clamped_to_limits():
    """Peso nunca ultrapassa 2.0 nem fica abaixo de 0.2."""
    ...

def test_source_order_changes_with_weights():
    """Fontes reordenadas corretamente pelo planner."""
    ...
```

---

## COMMIT FINAL

```bash
git add .
git commit --no-verify -m "feat: Fase 4 — personalização de fontes por histórico de feedback do usuário"
```

---

## STATUS ESPERADO AO FINALIZAR

| Critério | Esperado |
|----------|----------|
| `FeedbackStore.record_source_feedback()` implementado | ✅ |
| `FeedbackStore.get_source_weights()` implementado | ✅ |
| `SourcePlanner._apply_user_weights()` integrado | ✅ |
| Tool MCP `record_feedback` rastreia fonte | ✅ |
| Documentação `docs/PRIVACY_FEEDBACK.md` criada | ✅ |
| 6+ testes unitários passando | ✅ |
| Cold start retorna pesos neutros | ✅ |
| Commit na branch `main` | ✅ |
