# MISSÃO CLAUDE — Fase 2: Roteamento Dinâmico (LLM-Driven Universal Router)

**Projeto:** `E:\Meus LLMs\smart-research-agent`
**Pré-requisito:** Fase 0+1+3A já concluídas e em `main`. Execute `git pull origin main` antes de começar.

---

## CONTEXTO

O `SourcePlanner` atual decide as fontes de busca por uma **tabela estática** (`domains.yaml`) com apenas 7 domínios tech fixos. Qualquer query fora desse recorte cai em `general`, que só aponta para fontes técnicas. Existe um prompt `prompts/source_planner.md` pronto para decisão via LLM — mas o código real (`src/source_planner.py`) não o usa.

O objetivo desta missão é ativar o roteamento dinâmico via LLM como **complemento** (não substituição) da tabela estática.

---

## LEIA ANTES DE TUDO

1. `E:\Meus LLMs\Conversa\PLANO_SRA_BUSCA_UNIVERSAL.md` — Seção "Fase 2"
2. `E:\Meus LLMs\CLAUDE.md` — Governança, skills e protocolo de boot
3. `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md`
4. `E:\Meus LLMs\.claude\skills\python-patterns\SKILL.md`
5. `E:\Meus LLMs\.claude\skills\prompt-engineering\SKILL.md`

---

## SKILLS OBRIGATÓRIAS

Carregue ANTES de escrever qualquer código:
- `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md`
- `E:\Meus LLMs\.claude\skills\python-patterns\SKILL.md`
- `E:\Meus LLMs\.claude\skills\prompt-engineering\SKILL.md`

---

## TAREFAS (ORDEM OBRIGATÓRIA)

### TAREFA 1 — Adicionar domínio `universal` no `domains.yaml`
**Arquivo:** `config/domains.yaml`

Adicione ao final do arquivo um novo domínio:
```yaml
universal:
  description: "Domínio de fallback para queries sem correspondência técnica específica"
  primary:
    - wikipedia
    - duckduckgo
    - searxng
    - web
  secondary:
    - hackernews
    - reddit
    - rss
    - firecrawl
  tertiary:
    - multilingual
    - scraping
```

**Critério de aceite:** `yaml.safe_load(open("config/domains.yaml"))["universal"]` não levanta exceção.

---

### TAREFA 2 — Ativar o LLM-driven Universal Router no `SourcePlanner`
**Arquivo:** `src/source_planner.py`

**Situação atual:** `SourcePlanner.plan()` faz lookup direto em `self.domain_map[domain_key]` sem usar LLM.

**O que implementar:**
1. Adicione o método `async def _plan_with_llm(self, intent: IntentResult, query: str) -> list[str]` que:
   - Lê o prompt base de `prompts/source_planner.md` (se o arquivo existir — caso contrário usa um prompt embutido como fallback)
   - Injeta `{domain}`, `{intent}`, `{query}` e a lista de searchers disponíveis no prompt
   - Chama `self.llm.generate(prompt)` e parseia a resposta como lista de nomes de fontes
   - Retorna apenas nomes que existam de fato no `SearcherFactory` (valida antes de retornar)

2. No `SourcePlanner.plan()`, aplique a lógica:
   - Se `domain_key` estiver no `domain_map` e não for `universal` → usa roteamento estático (comportamento atual preservado)
   - Se `domain_key` for `universal` ou não encontrado → chama `_plan_with_llm()` e mescla resultado com as fontes de `universal` do yaml
   - Fallback final: se `_plan_with_llm()` falhar ou retornar lista vazia → usa `universal` do yaml como está

3. Se `self.llm` for `None` (ex: modo offline), pula o LLM e usa direto o yaml — nunca lançar exceção por falta de LLM.

**Prompt embutido de fallback** (adicionar como constante `UNIVERSAL_PLANNER_PROMPT` no topo do módulo):
```python
UNIVERSAL_PLANNER_PROMPT = """Você é um planejador de fontes de pesquisa.

Query: {query}
Domínio detectado: {domain}
Intenção: {intent}
Fontes disponíveis: {available_sources}

Liste as 3-6 fontes mais relevantes para responder esta query.
Responda APENAS com os nomes das fontes separados por vírgula, ex: wikipedia, duckduckgo, reddit
Sem explicação, sem markdown."""
```

**Critério de aceite:**
```python
# Query técnica → usa roteamento estático, não chama LLM
planner.plan(intent_dev_tools, "best python orm")  # usa github, hackernews etc

# Query genérica → cai em universal
planner.plan(intent_general, "receitas de bolo de chocolate")  # usa wikipedia, duckduckgo etc
```

---

### TAREFA 3 — Criar/Atualizar o prompt `prompts/source_planner.md`
**Arquivo:** `prompts/source_planner.md`

Crie (ou substitua) o arquivo com este conteúdo profissional:
```markdown
# Source Planner — Universal Router

Você é um especialista em curadoria de fontes de informação.

**Query do usuário:** {query}
**Domínio identificado pelo sistema:** {domain}
**Intenção:** {intent}
**Fontes disponíveis:** {available_sources}

## Sua tarefa

Selecione as **3 a 6 fontes** mais adequadas para responder esta query com qualidade.

## Regras de seleção

- Prefira fontes com cobertura direta do tópico
- Para fatos/definições: inclua `wikipedia`
- Para código/projetos: inclua `github`
- Para tendências/opiniões: inclua `reddit` ou `hackernews`
- Para buscas genéricas/abertas: inclua `duckduckgo` ou `searxng`
- Não selecione mais de 6 fontes
- Use apenas nomes da lista {available_sources}

## Formato de resposta

Responda APENAS com os nomes separados por vírgula:
wikipedia, duckduckgo, reddit
```

---

### TAREFA 4 — Testes unitários
**Arquivo:** `tests/test_source_planner_llm.py`

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_universal_domain_calls_llm():
    """Verifica que domínio universal aciona o LLM router."""
    from src.source_planner import SourcePlanner
    
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(return_value="wikipedia, duckduckgo, reddit")
    
    planner = SourcePlanner.__new__(SourcePlanner)
    planner.llm = mock_llm
    planner.domain_map = {"universal": {"primary": ["wikipedia"], "secondary": []}}
    
    intent = MagicMock()
    intent.domain = MagicMock()
    intent.domain.value = "universal"
    intent.intent = "discover"
    
    sources = await planner._plan_with_llm(intent, "receitas de bolo")
    mock_llm.generate.assert_called_once()
    assert isinstance(sources, list)

@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_yaml():
    """Verifica que falha do LLM não quebra o planner."""
    from src.source_planner import SourcePlanner
    
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(side_effect=Exception("LLM offline"))
    
    planner = SourcePlanner.__new__(SourcePlanner)
    planner.llm = mock_llm
    planner.domain_map = {"universal": {"primary": ["wikipedia", "duckduckgo"], "secondary": []}}
    
    intent = MagicMock()
    intent.domain = MagicMock()
    intent.domain.value = "universal"
    intent.intent = "discover"
    
    # Não deve lançar exceção
    sources = await planner._plan_with_llm(intent, "qualquer query")
    assert isinstance(sources, list)

def test_none_llm_does_not_raise():
    """Verifica que planner sem LLM não quebra."""
    from src.source_planner import SourcePlanner
    
    planner = SourcePlanner.__new__(SourcePlanner)
    planner.llm = None
    planner.domain_map = {"universal": {"primary": ["wikipedia"], "secondary": []}}
    # Deve funcionar sem LLM (modo offline)
    assert planner.llm is None
```

---

### TAREFA 5 — Validação Final
Execute:
```bash
pytest tests/test_source_planner_universal.py tests/test_source_planner_llm.py -v
```
Todos os testes devem passar.

---

## COMMIT FINAL

```bash
git add .
git commit --no-verify -m "feat: roteamento dinâmico LLM + domínio universal no SourcePlanner (Fase 2)"
```

---

## STATUS ESPERADO AO FINALIZAR

| Critério | Esperado |
|----------|----------|
| `domains.yaml` tem bloco `universal` | ✅ |
| `SourcePlanner._plan_with_llm()` implementado | ✅ |
| `prompts/source_planner.md` atualizado | ✅ |
| Roteamento estático preservado para domínios tech | ✅ |
| Testes passando | ✅ |
| Commit na branch `main` | ✅ |
