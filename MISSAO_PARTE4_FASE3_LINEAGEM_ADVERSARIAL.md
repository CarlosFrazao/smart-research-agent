# MISSÃO PARTE4 — FASE 3: Linhagem de Citação + Passada Adversarial + Seção de Confiança

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Pré-requisito: **Fases 1 e 2 concluídas** (`published_at` + fontes de notícia ativas).
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — AS TRÊS IDEIAS QUE TORNAM O SRA SUPERIOR, NÃO SÓ MAIS AMPLO

Mais fontes resolvem **cobertura**. Estas três melhorias atacam **confiabilidade**:

1. **Linhagem de citação (§15.1):** hoje o clustering mostra "confirmado por 10 fontes" quando podem ser 9 sites reproduzindo a mesma agência. A linhagem expõe isso: "1 fonte primária + 9 derivadas".
2. **Passada adversarial leve (§15.2):** uma única query extra formulada deliberadamente para desafiar a conclusão emergente — reduz viés de confirmação sem o custo do modo debate completo.
3. **Seção de confiança por afirmação (§15.3):** relatório passa a dizer claramente o que pode ser confiado e o que precisa de verificação adicional, baseado nos dados das correções acima.

As três se dependem mutuamente: linhagem alimenta a seção de confiança; passada adversarial também alimenta. Implementar juntas.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | Alterações em types.py, pipeline stages |
| `clean-code` | `E:\Meus LLMs\.claude\skills\clean-code\SKILL.md` | Garantir backward compat e fallbacks seguros |
| `test-driven-development` | `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md` | Testes de linhagem e passada adversarial |
| `prompt-engineering` | `E:\Meus LLMs\.claude\skills\prompt-engineering\SKILL.md` | Formular a query adversarial de forma eficaz |

---

## 📋 TAREFAS (em ordem obrigatória)

### TAREFA 3.1 — Adicionar campos de linhagem em `SearchResult`

**Arquivo:** `src/types.py`

Adicionar logo após os campos de cluster (`cluster_id`, `corroborated_by`):

```python
lineage_role: Literal["primary", "derivative", "unknown"] = "unknown"
# "primary": provável origem — o mais antigo do cluster, ou o que é citado pelos demais.
# "derivative": reproduz/cita outro item do mesmo cluster.
# "unknown": fallback seguro quando não é possível determinar.

cites_within_cluster: list[str] = Field(default_factory=list)
# IDs de outros SearchResults do mesmo cluster que este item cita diretamente no texto.
```

---

### TAREFA 3.2 — Implementar detecção de linhagem em novo stage

**Arquivo a criar:** `src/pipeline/stages/lineage_stage.py`

```python
class LineageStage:
    """Detecta linhagem de citação dentro de cada cluster de resultados.
    Roda APÓS o cluster_similar_results() do RankStage e ANTES do SynthesizeStage.
    Zero chamadas LLM na via rápida — só heurística de URL + data.
    """
    
    def execute(self, context: PipelineContext) -> None:
        clusters = self._group_by_cluster(context.ranked_results)
        for cluster_id, members in clusters.items():
            if len(members) <= 1:
                continue
            self._classify_lineage(members)
    
    def _classify_lineage(self, members: list) -> None:
        # 1. Ordenar por published_at (mais antigo = candidato a primary)
        sorted_members = sorted(members, key=lambda r: getattr(getattr(r, "result", r), "published_at", None) or datetime.max)
        primary_candidate = sorted_members[0]
        primary_result = getattr(primary_candidate, "result", primary_candidate)
        primary_result.lineage_role = "primary"
        primary_domain = self._extract_domain(primary_result.url)
        
        # 2. Verificar citação nos demais (regex de domínio no texto)
        for member in sorted_members[1:]:
            result = getattr(member, "result", member)
            text = f"{result.description or ''} {getattr(result, 'raw', '') or ''}"
            if primary_domain and primary_domain in text:
                result.cites_within_cluster.append(primary_result.url)
                result.lineage_role = "derivative"
            else:
                result.lineage_role = "derivative"  # default: deriva do primário por ser mais novo

    def _extract_domain(self, url: str) -> str | None:
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc.replace("www.", "")
        except Exception:
            return None
```

Registrar `LineageStage` no `stage_factory.py` logo após o `RankStage`.

---

### TAREFA 3.3 — Implementar passada adversarial no pipeline

**Arquivo:** `src/pipeline/stages/expand_stage.py` (ou criar `src/pipeline/stages/adversarial_stage.py`)

Roda após o `RankStage`, antes do `SynthesizeStage`, **apenas quando `enable_adversarial_pass=True`**:

```python
class AdversarialPassStage:
    """Gera uma query adversarial única para combater viés de confirmação.
    Custo: 1 query + 1 rodada de busca extra.
    Ativa por padrão nos modos: cirurgia, arqueologia, black_ops.
    """
    
    async def execute(self, context: PipelineContext) -> None:
        if not context.operation_mode.enable_adversarial_pass:
            return
        
        # Gerar query adversarial via LLM (prompt simples, sem scaffold complexo)
        adversarial_query = await self._generate_adversarial_query(
            original_query=context.query,
            emerging_conclusion=self._summarize_top_results(context.ranked_results[:5])
        )
        
        # Rodar busca com a query adversarial pelas mesmas fontes
        adversarial_results = await self._run_search(adversarial_query, context)
        
        # Injetar como evidência de primeira classe (não seção separada)
        for r in adversarial_results:
            r.result.is_adversarial = True  # flag para o ReportStage saber
        context.ranked_results.extend(adversarial_results)
    
    async def _generate_adversarial_query(self, original_query: str, emerging_conclusion: str) -> str:
        prompt = f"""Dada a query original: "{original_query}"
E a conclusão emergente dos resultados: "{emerging_conclusion}"

Formule UMA query que busque deliberadamente evidências CONTRA essa conclusão.
Use padrões como: "problemas com", "críticas a", "falhas de", "por que X está errado", etc.
Responda SOMENTE com a query, sem explicação."""
        return await self.llm_client.complete(prompt, max_tokens=60)
```

**Adicionar `enable_adversarial_pass: bool = False` em `OperationConfig` (`src/operation_modes.py`)**:
- Ligado por padrão nos modos: `cirurgia`, `arqueologia`, `black_ops`
- Desligado por padrão em `guerrilha`, `padrao`

Adicionar `is_adversarial: bool = False` em `SearchResult` (`src/types.py`).

---

### TAREFA 3.4 — Adicionar seção "Nível de Confiança" no ReportGenerator

**Arquivo:** `src/report_generator.py` (ou onde as seções do relatório são montadas)

Adicionar nova seção `_build_confidence_section()` chamada no final do relatório:

```python
def _build_confidence_section(self, results: list, adversarial_hits: list) -> str:
    """Gera a seção ⚠️ Nível de Confiança por Afirmação."""
    low_confidence_claims = []
    
    for r in results:
        result = getattr(r, "result", r)
        
        # Claims sem fonte primária no cluster
        if getattr(result, "lineage_role", "unknown") == "unknown" and not getattr(result, "cites_within_cluster", []):
            low_confidence_claims.append(f"- ⚠️ **{result.title[:80]}** — sem confirmação independente de fonte primária verificável")
        
        # Claims contestadas pela passada adversarial
        if getattr(result, "is_adversarial", False):
            low_confidence_claims.append(f"- 🔄 **Ponto de vista alternativo encontrado** — evidência contrária detectada na busca adversarial")
    
    if not low_confidence_claims:
        return ""
    
    lines = ["## ⚠️ Nível de Confiança por Afirmação\n",
             "> As afirmações abaixo requerem verificação adicional antes de serem tomadas como fato:\n"]
    lines.extend(low_confidence_claims)
    return "\n".join(lines)
```

---

### TAREFA 3.5 — Testes unitários

**Arquivo a criar:** `tests/test_lineage_adversarial.py`

Cobrir:
1. `LineageStage` classifica o resultado mais antigo de um cluster como `"primary"`.
2. Resultado que contém o domínio do primário no texto recebe `"derivative"` e `cites_within_cluster` populado.
3. `AdversarialPassStage` não roda em modo `guerrilha` (desligado).
4. `AdversarialPassStage` roda em modo `cirurgia` e injeta resultados com `is_adversarial=True`.
5. `_build_confidence_section` gera a seção quando há claims com `lineage_role == "unknown"`.

---

### TAREFA 3.6 — Commit

```bash
git add src/types.py src/pipeline/stages/lineage_stage.py \
        src/pipeline/stages/adversarial_stage.py src/operation_modes.py \
        src/report_generator.py tests/test_lineage_adversarial.py
git commit -m "feat(parte4/fase3): linhagem de citação + passada adversarial + seção de confiança"
git push origin main
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 3

- [ ] `SearchResult.lineage_role` e `cites_within_cluster` adicionados em `src/types.py`
- [ ] `SearchResult.is_adversarial: bool = False` adicionado
- [ ] `LineageStage` implementado e registrado no pipeline após `RankStage`
- [ ] `AdversarialPassStage` implementado com `enable_adversarial_pass` em `OperationConfig`
- [ ] Modos `cirurgia`, `arqueologia`, `black_ops` com `enable_adversarial_pass=True`
- [ ] Seção `## ⚠️ Nível de Confiança por Afirmação` adicionada ao `ReportGenerator`
- [ ] `tests/test_lineage_adversarial.py` — todos os testes verdes
- [ ] `python -m pytest tests/ --tb=short -q` — zero novas regressões
- [ ] Commit e push realizados

---

## 🚫 FORA DO ESCOPO DESTA FASE

- Verificação de linhagem via LLM (custo alto) — a heurística de URL/data é suficiente.
- Integrar `MultiLLMFactChecker` completo — está sendo religado na Fase 6 do Plano 3.
- Alterar o schema de exportação (BibTeX/RIS) para incluir `lineage_role`.
