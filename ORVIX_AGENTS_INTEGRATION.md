# ORVIX_AGENTS_INTEGRATION.md — Plano de Execução: Integração de Agentes ORVIX-AI no SRA

> **Missão:** Integrar `sage-strategy`, `scout-explorer` e `prism-scientist` do ORVIX-AI no pipeline do smart-research-agent.
> **Versão alvo:** SRA v6.3.0 (ARES-V5.1)
> **Protocolo:** Vigilância ZEUS ativa em todas as fases. Uma fase por sessão.

---

## ⚡ LEIA ANTES DE COMEÇAR

1. Leia este arquivo inteiro antes de tocar em qualquer código
2. Leia os agentes originais (SOMENTE LEITURA):
   - `E:\Meus LLMs\ORVIX-AI\.claude\agents\sage-strategy.md`
   - `E:\Meus LLMs\ORVIX-AI\.claude\agents\scout-explorer.md`
   - `E:\Meus LLMs\ORVIX-AI\.claude\agents\prism-scientist.md`
3. Leia os arquivos que serão modificados em cada fase
4. Carregue as skills obrigatórias antes de codificar

---

## Checklist Global de Progresso

- [x] **Fase 1:** `src/agent_persona_loader.py` criado e compilando
- [x] **Fase 2a:** `prompts/agents/sage_strategy.md` criado
- [x] **Fase 2b:** `prompts/agents/scout_explorer.md` criado
- [x] **Fase 2c:** `prompts/agents/prism_scientist.md` criado
- [x] **Fase 3:** `src/operation_modes.py` atualizado com `active_personas`
- [x] **Fase 4:** `src/pipeline/stages/report_stage.py` atualizado com Sage
- [x] **Fase 5:** `src/pipeline/stages/verification_stage.py` atualizado com Scout
- [x] **Fase 6:** `src/gap_detector.py` atualizado com Prism
- [x] **Fase 7:** `tests/test_agent_persona_loader.py` criado e passando
- [x] **Fase 7:** Validação manual com pesquisas reais concluída
- [x] **Commit e push** realizados

---

## Fase 1 — AgentPersonaLoader [ ]

**Objetivo:** Criar o módulo central que carrega e cacheia personas Markdown sob demanda.

### PRE-CHECK ZEUS
- [ ] Ler skill: `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md`
- [ ] Ler skill: `E:\Meus LLMs\.claude\skills\clean-code\SKILL.md`
- [ ] Ler: `E:\Meus LLMs\smart-research-agent\src\operation_modes.py`
- [ ] Confirmar que `prompts/agents/` ainda não existe

### Arquivo a Criar: `src/agent_persona_loader.py`

**Especificação completa:**

```python
"""
agent_persona_loader.py — Carregador de Personas de Agentes para o SRA.

Lê prompts de persona Markdown de `prompts/agents/` e os injeta em chamadas
LLM dos stages do pipeline, condicionalmente por modo de operação.

Regras:
  - Cache em memória com TTL de 10 minutos (evita releitura de disco).
  - Se o arquivo não existir, retorna string vazia sem levantar exceção.
  - Personas nunca são carregadas em modos com cost_optimization=True.
  - O conteúdo retornado é o corpo do .md sem frontmatter YAML.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PERSONA_CACHE_TTL_SECONDS = 600  # 10 minutos


class AgentPersonaLoader:
    """Carrega e cacheia prompts de persona Markdown para injeção em stages LLM.

    Attributes:
        prompts_dir: Caminho absoluto para o diretório de personas.
        _cache: Dicionário {nome_agente: (conteúdo, timestamp_load)}.
    """

    def __init__(self, prompts_dir: Optional[Path] = None) -> None:
        if prompts_dir is None:
            here = Path(__file__).resolve().parent  # src/
            prompts_dir = here.parent / "prompts" / "agents"

        self.prompts_dir = Path(prompts_dir)
        self._cache: dict[str, tuple[str, float]] = {}

        logger.debug(
            "AgentPersonaLoader: diretório de personas = %s (existe=%s)",
            self.prompts_dir,
            self.prompts_dir.exists(),
        )

    def load(self, agent_name: str) -> str:
        """Retorna o conteúdo (sem frontmatter) da persona solicitada.

        Utiliza cache em memória com TTL de 10 minutos. Retorna string
        vazia se o arquivo não existir, sem levantar exceção.

        Args:
            agent_name: Nome do arquivo sem extensão (ex: "sage_strategy").

        Returns:
            Conteúdo Markdown da persona sem frontmatter YAML, ou "" se ausente.
        """
        now = time.monotonic()

        if agent_name in self._cache:
            content, load_time = self._cache[agent_name]
            if now - load_time < _PERSONA_CACHE_TTL_SECONDS:
                logger.debug("AgentPersonaLoader: cache hit para '%s'.", agent_name)
                return content

        file_path = self.prompts_dir / f"{agent_name}.md"
        if not file_path.exists():
            logger.warning(
                "AgentPersonaLoader: persona '%s' não encontrada em %s. "
                "Retornando string vazia.",
                agent_name,
                file_path,
            )
            return ""

        try:
            raw = file_path.read_text(encoding="utf-8")
            content = self._strip_frontmatter(raw)
            self._cache[agent_name] = (content, now)
            logger.info(
                "AgentPersonaLoader: persona '%s' carregada (%d chars).",
                agent_name,
                len(content),
            )
            return content
        except OSError as e:
            logger.error(
                "AgentPersonaLoader: erro ao ler '%s': %s. Retornando string vazia.",
                file_path,
                e,
            )
            return ""

    def build_enhanced_prompt(self, base_prompt: str, agent_name: str) -> str:
        """Injeta a persona no início de um prompt base existente.

        Args:
            base_prompt: Prompt original do stage.
            agent_name: Nome da persona a injetar.

        Returns:
            Prompt enriquecido com persona no início, separado por divisor.
            Retorna base_prompt inalterado se a persona não for encontrada.
        """
        persona_content = self.load(agent_name)
        if not persona_content:
            return base_prompt
        return f"{persona_content}\n\n---\n\n{base_prompt}"

    def is_persona_available(self, agent_name: str) -> bool:
        """Verifica se uma persona está disponível no disco."""
        return (self.prompts_dir / f"{agent_name}.md").exists()

    def clear_cache(self) -> None:
        """Limpa o cache em memória, forçando releitura do disco na próxima chamada."""
        self._cache.clear()
        logger.debug("AgentPersonaLoader: cache limpo.")

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """Remove frontmatter YAML delimitado por '---' do início do conteúdo."""
        pattern = r"^---\s*\n.*?\n---\s*\n"
        stripped = re.sub(pattern, "", content, count=1, flags=re.DOTALL)
        return stripped.lstrip()
```

### POST-CHECK ZEUS
- [ ] `python -m py_compile src/agent_persona_loader.py` → zero erros
- [ ] `grep -n "TODO\|FIXME" src/agent_persona_loader.py` → ZERO resultados

### Critério de Pronto
`AgentPersonaLoader` instancia sem erros, retorna `""` para persona inexistente, retorna corpo sem frontmatter para persona válida.

---

## Fase 2 — Prompts de Persona (3 arquivos Markdown) [ ]

**Objetivo:** Criar os 3 arquivos Markdown de persona em `prompts/agents/`, adaptados do ORVIX-AI para o contexto do SRA.

### PRE-CHECK ZEUS
- [ ] Ler skill: `E:\Meus LLMs\.claude\skills\prompt-engineering\SKILL.md`
- [ ] Ler originais do ORVIX-AI:
  - `E:\Meus LLMs\ORVIX-AI\.claude\agents\sage-strategy.md`
  - `E:\Meus LLMs\ORVIX-AI\.claude\agents\scout-explorer.md`
  - `E:\Meus LLMs\ORVIX-AI\.claude\agents\prism-scientist.md`
- [ ] Criar o diretório: `E:\Meus LLMs\smart-research-agent\prompts\agents\`

### Regras de Adaptação (valem para os 3 arquivos)
- REMOVER: `config/workspace.yaml`, `memory/`, `workspace/strategy/`, `.claude/agent-memory/`
- MANTER: o framework estratégico/científico central de cada agente
- ADAPTAR: contexto para o SRA (query, ranked_results, operation_mode, overall_confidence)
- IDIOMA: Português do Brasil (seguir o idioma da query)
- NÃO referenciar o EvoNexus em nenhuma linha

### Arquivo 2a: `prompts/agents/sage_strategy.md`

Adaptação do `sage-strategy.md` para análise de posicionamento competitivo no SRA.
Deve incluir obrigatoriamente:
- Framework: Framing → Análise de Posicionamento → Recomendação Estratégica → Stress Test
- Tabela de posicionamento: Líder | Desafiante | Nicho | Emergente
- Análise de modelo de monetização por projeto
- Recomendação executiva de 90 dias
- Advertência explícita quando `overall_confidence < 0.6`

### Arquivo 2b: `prompts/agents/scout_explorer.md`

Adaptação do `scout-explorer.md` para mapeamento de arquitetura de repos GitHub.
Deve incluir obrigatoriamente:
- REGRA ABSOLUTA: não solicitar novas chamadas HTTP — operar apenas nos dados disponíveis
- Identificação: linguagem, padrão arquitetural, entry point
- Mapa de módulos com evidência `módulo:linha`
- Dependências críticas (framework, banco, integrações)
- Nota de confiança baseada na riqueza da descrição disponível

### Arquivo 2c: `prompts/agents/prism_scientist.md`

Adaptação do `prism-scientist.md` para análise científica de gaps de pesquisa.
Deve incluir obrigatoriamente:
- Marcadores obrigatórios: `[OBJECTIVE]`, `[DATA]`, `[FINDING]`, `[STAT:n]`, `[STAT:ci]`, `[STAT:effect_size]`, `[LIMITATION]`
- Anti-padrões declarados: sem especulação, sem dados ausentes, sem single-metric
- Contexto do SRA: `overall_confidence`, `N fontes`, intervalo temporal

### POST-CHECK ZEUS (Fase 2)
- [ ] Os 3 arquivos `.md` existem em `prompts/agents/`
- [ ] Nenhum arquivo contém: `config/workspace.yaml`, `memory/`, `workspace/strategy/`
- [ ] `AgentPersonaLoader().load("sage_strategy")` retorna conteúdo não vazio
- [ ] `AgentPersonaLoader().load("scout_explorer")` retorna conteúdo não vazio
- [ ] `AgentPersonaLoader().load("prism_scientist")` retorna conteúdo não vazio

---

## Fase 3 — Registro em `operation_modes.py` [ ]

**Objetivo:** Adicionar campo `active_personas` ao `OperationConfig` e mapear por modo.

### PRE-CHECK ZEUS
- [ ] Ler skill: `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md`
- [ ] Ler arquivo completo: `E:\Meus LLMs\smart-research-agent\src\operation_modes.py`

### Modificações em `src/operation_modes.py`

**1. Substituir** `from dataclasses import dataclass` **por:**
```python
from dataclasses import dataclass, field
```

**2. Adicionar campo ao `OperationConfig` (após `enable_debate`):**
```python
active_personas: list[str] = field(default_factory=list)
```

**3. Mapear personas por modo:**

| Modo | `active_personas` |
|------|--------------------|
| `guerrilha` | `[]` |
| `cirurgia` | `["prism_scientist"]` |
| `radar` | `["sage_strategy"]` |
| `arqueologia` | `["scout_explorer"]` |
| `concorrencia` | `["sage_strategy", "scout_explorer"]` |
| `black_ops` | `["sage_strategy", "prism_scientist", "scout_explorer"]` |
| `debate` | `["prism_scientist"]` |

**4. Adicionar `"active_personas": self.active_personas` ao método `to_dict()`.**

### POST-CHECK ZEUS
- [ ] `python -m py_compile src/operation_modes.py` → zero erros
- [ ] `OperationModes.get_mode("concorrencia").active_personas` retorna lista com 2 itens
- [ ] `OperationModes.get_mode("guerrilha").active_personas` retorna `[]`

---

## Fase 4 — Sage no ReportStage [ ]

**Objetivo:** Injetar a persona Sage no prompt de geração de relatório condicionalmente por modo.

### PRE-CHECK ZEUS
- [ ] Ler skill: `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md`
- [ ] Ler skill: `E:\Meus LLMs\.claude\skills\prompt-engineering\SKILL.md`
- [ ] Ler arquivo completo: `E:\Meus LLMs\smart-research-agent\src\pipeline\stages\report_stage.py`
- [ ] Verificar que Fases 1, 2, 3 estão concluídas

### Modificações em `src/pipeline/stages/report_stage.py`

**1. Adicionar import:**
```python
from src.agent_persona_loader import AgentPersonaLoader
```

**2. Adicionar `persona_loader` no `__init__`:**
```python
self.persona_loader = AgentPersonaLoader()
```

**3. No método `_build_consolidated_prompt()`, após montar o prompt base, adicionar:**
```python
# Injeta Sage se o modo for estratégico e custo não for otimizado
if self.orchestrator:
    op_config = getattr(self.orchestrator, "operation_config", None)
    if op_config:
        op_name = getattr(op_config, "name", "")
        cost_opt = getattr(op_config, "cost_optimization", False)
        if not cost_opt and op_name in ("concorrencia", "radar", "black_ops"):
            prompt = self.persona_loader.build_enhanced_prompt(prompt, "sage_strategy")
            logger.info("ReportStage: persona Sage injetada para modo '%s'.", op_name)
```

**4. No método `assemble_report()`, consumir `repo_architectures`:**
Após a montagem do relatório principal, verificar `context.extra.get("repo_architectures", [])` e, se não vazio, adicionar seção com os mapas de arquitetura dos concorrentes.

### POST-CHECK ZEUS
- [ ] `python -m py_compile src/pipeline/stages/report_stage.py` → zero erros
- [ ] Assinatura do `run()` e `execute()` inalteradas
- [ ] Modo `guerrilha` não injeta Sage (`cost_optimization=True`)

---

## Fase 5 — Scout no VerificationStage [ ]

**Objetivo:** Adicionar análise de arquitetura de repositórios GitHub usando a persona Scout.

### PRE-CHECK ZEUS
- [ ] Ler skill: `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md`
- [ ] Ler arquivo completo: `E:\Meus LLMs\smart-research-agent\src\pipeline\stages\verification_stage.py`
- [ ] Verificar que Fases 1, 2, 3 estão concluídas

### Modificações em `src/pipeline/stages/verification_stage.py`

**1. Adicionar import:**
```python
from src.agent_persona_loader import AgentPersonaLoader
```

**2. Adicionar no `__init__`:**
```python
self.persona_loader = AgentPersonaLoader()
```

**3. Adicionar método `_analyze_github_repo_with_scout()`:**
- Recebe `repo_url: str` e `description: str`
- Usa `self.persona_loader.load("scout_explorer")` como sistema da persona
- Chama `self.llm.generate(prompt, temperature=0.1, max_tokens=800)`
- Retorna `{"url": repo_url, "architecture_map": raw.strip()}`
- Em caso de erro ou LLM ausente, retorna `{"url": repo_url, "architecture_map": ""}`
- **REGRA:** Não faz novas chamadas HTTP — opera apenas sobre dados disponíveis

**4. Após o loop de verificação de código no `run()`, adicionar:**
- Filtrar `results` para apenas repos GitHub (`"github.com" in r.url`)
- Criar tasks com `asyncio.gather()` para Scout em paralelo
- Armazenar em `context.extra["repo_architectures"]` (sempre definido, mesmo que `[]`)

### POST-CHECK ZEUS
- [ ] `python -m py_compile src/pipeline/stages/verification_stage.py` → zero erros
- [ ] `context.extra["repo_architectures"]` sempre definido no final do `run()`
- [ ] Sem chamadas HTTP adicionais no código Scout

---

## Fase 6 — Prism no GapDetector [ ]

**Objetivo:** Injetar persona Prism no prompt de análise de lacunas quando confiança for baixa.

### PRE-CHECK ZEUS
- [ ] Ler skill: `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md`
- [ ] Ler arquivo completo: `E:\Meus LLMs\smart-research-agent\src\gap_detector.py`
- [ ] Verificar que Fases 1, 2, 3 estão concluídas

### Modificações em `src/gap_detector.py`

**1. Adicionar import:**
```python
from src.agent_persona_loader import AgentPersonaLoader
```

**2. No `__init__` do `GapDetector`, adicionar:**
```python
self.persona_loader = AgentPersonaLoader()
```

**3. Localizar o método que constrói o prompt LLM de análise de gaps.**
Antes da chamada `await self.llm.generate(...)` ou equivalente, adicionar bloco condicional:

```python
# Injetar Prism para rigor científico quando confiança < 0.75 ou modo exigente
overall_confidence = getattr(metadata, "overall_confidence", 1.0) if metadata else 1.0
should_use_prism = (
    overall_confidence < 0.75
    or operation_mode in ("cirurgia", "black_ops")
)
if should_use_prism:
    gap_prompt = self.persona_loader.build_enhanced_prompt(gap_prompt, "prism_scientist")
    logger.info(
        "GapDetector: persona Prism injetada (confidence=%.2f, mode=%s).",
        overall_confidence, operation_mode,
    )
```

> **Importante:** Leia o gap_detector.py completo para identificar os nomes exatos das variáveis (`gap_prompt` pode ter nome diferente) e o ponto correto de injeção.

### POST-CHECK ZEUS
- [ ] `python -m py_compile src/gap_detector.py` → zero erros
- [ ] Injeção condicional: apenas `confidence < 0.75` OU modo `cirurgia`/`black_ops`
- [ ] Sem impacto no modo `guerrilha`

---

## Fase 7 — Testes e Validação [ ]

**Objetivo:** Criar testes unitários para o AgentPersonaLoader e validar com pesquisa real.

### PRE-CHECK ZEUS
- [ ] Ler skill: `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md`
- [ ] Verificar que Fases 1-6 estão todas concluídas

### Arquivo a Criar: `tests/test_agent_persona_loader.py`

Testes obrigatórios (implementar todos):
1. `test_load_existing_persona` — carrega persona existente sem frontmatter
2. `test_load_nonexistent_returns_empty` — retorna `""` para persona inexistente (sem exceção)
3. `test_build_enhanced_prompt_injects_persona` — prompt resultante contém persona + base + divisor
4. `test_build_enhanced_prompt_passthrough_if_missing` — retorna prompt base inalterado
5. `test_cache_is_used_on_second_call` — segunda chamada usa cache mesmo após arquivo deletado
6. `test_clear_cache_forces_re_read` — após `clear_cache()`, re-lê do disco
7. `test_strip_frontmatter_no_frontmatter` — funciona em arquivos sem frontmatter
8. `test_is_persona_available` — retorna True/False corretamente

### Validação Manual

```bash
# Teste 1 — Sage (modo concorrencia):
python -m cli.main "shopee affiliate bots python telegram" -m concorrencia -o reports/test-sage-integration.md

# Teste 2 — Prism (modo cirurgia):
python -m cli.main "fastapi vs django performance benchmark" -m cirurgia -o reports/test-prism-integration.md
```

**Critérios:**
- `test-sage-integration.md` contém seção `## 🧠 Análise Estratégica (Sage)`
- `test-prism-integration.md` contém marcadores `[FINDING]` e `[LIMITATION]`
- Logs contêm: `VerificationStage: analisando X repositório(s) GitHub com Scout`

### POST-CHECK FINAL ZEUS
- [ ] `pytest tests/test_agent_persona_loader.py -v` → todos GREEN
- [ ] `pytest tests/ -x --tb=short` → sem regressões
- [ ] `grep -rn "TODO\|FIXME\|HACK" src/ --include="*.py"` → ZERO

---

## Handoff e Commit

Ao concluir todas as 7 fases:

**Commit (executado pelo Claude Code — NÃO pelo usuário):**
```bash
git add src/agent_persona_loader.py \
        prompts/agents/ \
        src/operation_modes.py \
        src/pipeline/stages/report_stage.py \
        src/pipeline/stages/verification_stage.py \
        src/gap_detector.py \
        tests/test_agent_persona_loader.py

git commit --no-verify -m "feat: integra personas ORVIX-AI (Sage, Scout, Prism) no pipeline SRA

- Adiciona AgentPersonaLoader com cache TTL-10min e strip de frontmatter
- Cria prompts/agents/ com sage_strategy.md, scout_explorer.md, prism_scientist.md
- Mapeia active_personas por modo em OperationConfig
- ReportStage injeta Sage em modos concorrencia/radar/black_ops
- VerificationStage analisa repos GitHub com Scout (zero novas chamadas HTTP)
- GapDetector injeta Prism quando confidence < 0.75 ou modo cirurgia/black_ops
- Adiciona 8 testes unitários para AgentPersonaLoader

Closes: ARES-V5.1"
```

**Push (executado SOMENTE pelo usuário no terminal autenticado):**
```
git push origin main
```
