# MISSÃO PARTE2 — FASE 5: Dependências + Prompts Órfãos + Dívida de Pyproject

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 5 do plano derivado da `AUDITORIA_SRA_PARTE_2.md`.
> Pré-requisito: **Fases 1, 2, 3 e 4 concluídas** (suíte completa passando).
> Execute SOMENTE o que está descrito aqui.

---

## 🎯 OBJETIVO DA FASE

Eliminar a dívida técnica de infraestrutura e organização:
1. Declarar dependências core que faltam no `pyproject.toml` (httpx, tiktoken, tenacity, redis)
2. Resolver os 5 prompts `.md` órfãos (arquivos de prompt que não são carregados por nenhum código)
3. Decidir o destino de `react_orchestrator.py` / `decision_engine.py` (arquitetura paralela desconectada)

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `.claude/skills/python-pro/SKILL.md` | Para `pyproject.toml` e estrutura de dependências |
| `clean-code` | `.claude/skills/clean-code/SKILL.md` | Para decisão sobre código morto (`react_orchestrator`) |

---

## 📋 TAREFAS

### TAREFA 5.1 — Declarar dependências core ausentes no `pyproject.toml`

**Arquivo alvo:** `pyproject.toml`

**Contexto:** `httpx`, `tiktoken`, `tenacity` e `redis` são usados em dezenas de arquivos em `src/`, mas não estão declarados em `dependencies` nem em nenhum `optional-dependencies`. Eles chegam hoje como dependência transitiva (httpx via anthropic/openai SDK, redis via chromadb), o que é frágil.

**O que fazer:**

1. Abrir `pyproject.toml` e localizar a seção `[project] dependencies = [...]` (ou equivalente).
2. Adicionar as quatro dependências com range conservador (compatível com o que o projeto já usa):
```toml
[project]
dependencies = [
    # ... dependências já existentes ...
    "httpx>=0.25.0",
    "tiktoken>=0.5.0",
    "tenacity>=8.2.0",
    "redis>=5.0.0",
]
```
> **Antes de adicionar:** abra cada um dos arquivos que importam essas libs e veja qual versão mínima é necessária. Use `import httpx; print(httpx.__version__)` no ambiente atual para confirmar a versão instalada.

3. (Opcional mas recomendado) Gerar um lockfile:
```bash
# Se o projeto usa uv:
uv lock

# Se usa pip:
pip-compile pyproject.toml -o requirements.lock
```

**Validação:**
```bash
# Instalar a partir do pyproject.toml modificado em um venv limpo:
python -m venv /tmp/sra_test_venv
/tmp/sra_test_venv/bin/pip install -e ".[dev]"
/tmp/sra_test_venv/bin/python -c "import httpx, tiktoken, tenacity, redis; print('all ok')"
```

---

### TAREFA 5.2 — Resolver os 5 prompts órfãos em `prompts/`

**Contexto:** Os seguintes arquivos em `prompts/` têm zero referências em código de produção:
- `prompts/query_expander.md`
- `prompts/ranker_system.md`
- `prompts/report_generator.md`
- `prompts/source_planner.md`
- `prompts/synthesizer.md`

Os módulos correspondentes (`src/synthesizer.py`, `src/report_generator.py`, etc.) constroem seus prompts de outra forma (provavelmente strings inline no `.py`).

**Decisão a tomar (para cada arquivo):**

Abrir o `.md` e o `.py` correspondente. Verificar se o conteúdo do `.md` é relevante ou está desatualizado em relação ao que o `.py` usa.

**Opção A — Migrar:** Fazer o `.py` carregar o prompt do `.md` (mais fácil de versionar e editar sem redeployar). Isso exige criar um loader (ou reusar o `agent_persona_loader.py` existente se for genérico o suficiente).

**Opção B — Marcar como referência:** Adicionar no topo de cada `.md` um comentário claro:
```markdown
<!-- AVISO: Este arquivo é documentação de referência do prompt de design.
     NÃO é carregado automaticamente por nenhum código Python.
     O prompt real está inline em src/[modulo].py.
     Para alterar o comportamento do LLM, edite o arquivo .py correspondente. -->
```

**Opção C — Remover:** Se o conteúdo do `.md` estiver completamente divergente do prompt real no `.py` e não tiver valor de documentação, simplesmente remover.

> **Recomendação:** Faça Opção B como mínimo (adicionar o aviso) mesmo que escolha Opção A para alguns arquivos. Nunca deixe um arquivo `.md` em `prompts/` sem sinalizar se ele é ou não a fonte de verdade.

Após a decisão, adicionar os arquivos processados ao **teste de reachability** (Tarefa 5.3).

---

### TAREFA 5.3 — Estender o teste de wiring para cobrir prompts (complemento da Fase 1)

**Arquivo alvo:** `tests/test_wiring_integration.py` (criado na Fase 1, expandir)

**Contexto:** O teste de reachability criado na Fase 1 cobre `BaseSearcher`/`BaseConnectorImplementation`/`PipelineStage`. Estendê-lo para cobrir também os arquivos `prompts/*.md`.

**O que fazer:**
```python
class TestPromptsWiring:
    """Valida que arquivos em prompts/ têm referência explícita no código ou são marcados como não-ativos."""

    KNOWN_UNLOADED_PROMPTS = {
        # Prompts documentados como não-carregados (Tarefa 5.2):
        # Adicione aqui os que foram marcados como Opção B
        "query_expander.md",
        "ranker_system.md",
        "report_generator.md",
        "source_planner.md",
        "synthesizer.md",
    }

    def test_unloaded_prompts_are_explicitly_documented(self):
        """
        Todo prompt não-carregado deve estar em KNOWN_UNLOADED_PROMPTS
        (i.e., a decisão foi tomada explicitamente).
        Se um novo .md for adicionado a prompts/ sem ser carregado pelo código,
        este teste vai falhar — forçando a decisão consciente.
        """
        import os
        prompts_dir = Path("prompts")
        if not prompts_dir.exists():
            pytest.skip("prompts/ directory not found")

        all_prompt_files = {f.name for f in prompts_dir.glob("*.md")}
        loaded_prompts = self._find_loaded_prompts()

        unloaded = all_prompt_files - loaded_prompts
        undocumented = unloaded - self.KNOWN_UNLOADED_PROMPTS

        assert not undocumented, (
            f"Prompts não-carregados sem decisão documentada: {sorted(undocumented)}\n"
            f"Adicione ao KNOWN_UNLOADED_PROMPTS (se intencional) ou "
            f"conecte ao código correspondente."
        )

    def _find_loaded_prompts(self) -> set[str]:
        """Busca referências a arquivos de prompt no código Python."""
        import subprocess
        result = subprocess.run(
            ["grep", "-rn", r"\.md", "src/", "--include=*.py", "-l"],
            capture_output=True, text=True
        )
        # Simplificação: retornar set vazio aqui e deixar o teste falhar com os não-documentados
        # A implementação real deve parsear os imports/opens de prompts
        return set()
```

---

### TAREFA 5.4 — Decidir o destino de `react_orchestrator.py` / `decision_engine.py`

**Arquivos alvo:** `src/react_orchestrator.py` e `src/decision_engine.py`

**Contexto:** Existe uma segunda arquitetura de orquestração completa (estilo ReAct), testada isoladamente em `tests/test_react_orchestrator.py`, mas nunca conectada ao `Orchestrator` principal usado por `api/main.py`/`cli/main.py`/`src/mcp_server.py`.

**Decisão a tomar:**

1. Ler `src/react_orchestrator.py` e `src/decision_engine.py` completamente.
2. Verificar os comentários/docstrings para qualquer indicação de intenção (WIP, experimental, futuro, etc.).
3. Escolher:
   - **Manter como WIP explícito:** Adicionar um arquivo `EXPERIMENTAL_MODULES.md` (ver abaixo) listando esses módulos com a intenção declarada e data de revisão.
   - **Integrar:** Conectar ao `Orchestrator` principal como uma estratégia alternativa (alta complexidade — só se houver intenção clara de usar).
   - **Remover:** Se for código morto sem valor futuro, remover para reduzir a superfície de manutenção.

**Se a decisão for "Manter como WIP"**, criar `EXPERIMENTAL_MODULES.md` na raiz:
```markdown
# Módulos Experimentais / WIP

Este arquivo documenta módulos que existem no repositório mas não estão
conectados ao pipeline principal. São candidatos a integração futura ou remoção.
Não são código morto — são trabalho em andamento ou experimentos intencionais.

## `src/react_orchestrator.py` + `src/decision_engine.py`
- **Status:** WIP experimental — arquitetura de orquestração estilo ReAct alternativa ao pipeline linear
- **Intenção:** [descrever aqui]
- **Revisão prevista:** [data ou sprint]
- **Referência de testes:** `tests/test_react_orchestrator.py`
```

**O teste de wiring da Fase 1 deve ser atualizado** para incluir esses módulos como exceção conhecida (padrão semelhante ao `KNOWN_UNLOADED_PROMPTS` da Tarefa 5.3).

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 5

- [ ] `httpx`, `tiktoken`, `tenacity`, `redis` em `pyproject.toml[dependencies]`
- [ ] Instalação em venv limpo funciona sem `ImportError`
- [ ] Todos os 5 arquivos de prompt em `prompts/` têm decisão documentada (Opção A, B ou C)
- [ ] Teste `TestPromptsWiring` em `test_wiring_integration.py` passando
- [ ] `react_orchestrator.py` / `decision_engine.py` têm destino declarado (WIP, integrar ou remover) + `EXPERIMENTAL_MODULES.md` se mantidos
- [ ] `python -m pytest tests/ --tb=short -q` → suíte completa sem novas falhas

---

## 🚫 FORA DO ESCOPO DESTA FASE

- `GenericAPISearcher` / `GenericWebsiteSearcher` → Fase 6
- Paridade CLI/API/UI → decisão de produto
- `scheduler.py` no CLI oficial → decisão de produto (Fase 6 se priorizado)
