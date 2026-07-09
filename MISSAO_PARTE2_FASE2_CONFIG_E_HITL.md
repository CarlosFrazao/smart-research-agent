# MISSÃO PARTE2 — FASE 2: Configuração Morta, HITL no-op e Exporters Órfãos

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 2 do plano derivado da `AUDITORIA_SRA_PARTE_2.md`.
> Pré-requisito: **Fase 1 concluída** (teste de wiring passando, conectores Enterprise registrados).
> Execute SOMENTE o que está descrito aqui.

---

## 🎯 OBJETIVO DA FASE

Eliminar "configuração morta" que induz erro ao editar, conectar os ramos HITL que hoje são no-op, e plugar exporters de citação (BibTeX/RIS) ao `report_generator.py`.

Estas tarefas não têm dependência entre si — podem ser feitas em qualquer ordem, mas todas devem ser concluídas antes de fechar a fase.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `.claude/skills/python-pro/SKILL.md` | Para todo código Python novo/editado |
| `clean-code` | `.claude/skills/clean-code/SKILL.md` | Para revisão de code smells ao editar `orchestrator.py` |
| `api-patterns` | `.claude/skills/api-patterns/SKILL.md` | Para o endpoint de feedback por resultado (se aplicável aqui) |

---

## 📋 TAREFAS

### TAREFA 2.1 — Conectar `config/scoring_weights.yaml` ao código OU marcá-lo como inativo

**Contexto:** `config/scoring_weights.yaml` e `config/sources.yaml` são **nunca lidos** por nenhum código. Se alguém editar esses arquivos esperando mudar comportamento, nada acontece. Isso é um risco real para quem for implementar as fases de expansão de fontes.

**Decisão a tomar (leia os dois arquivos antes de decidir):**

Abra `config/scoring_weights.yaml` e `config/sources.yaml`. Então abra `src/research_score.py` e `src/ranking/hybrid_ranker.py` para ver onde os pesos reais são definidos (hardcoded no Python).

**Opção A (conectar de verdade):** Criar um loader em `src/config.py` ou em um módulo dedicado `src/config/yaml_loader.py` que leia `scoring_weights.yaml` e exponha os valores para `hybrid_ranker.py`. Isso é mais trabalho, mas resolve de vez. Só faça se o conteúdo do YAML for coerente com o que o ranker realmente usa.

**Opção B (marcar como não-ativo):** Adicionar no topo de cada arquivo YAML:
```yaml
# AVISO: Este arquivo é documentação de referência de design, NÃO é carregado por nenhum código.
# Para alterar pesos reais, edite: src/ranking/hybrid_ranker.py (constante DEFAULT_BM25_WEIGHT, etc.)
# Issue de rastreamento: conectar este YAML ao código é tarefa futura planejada.
```
E adicionar ao `README.md` uma nota na seção de configuração explicando isso.

> **Recomendação:** Use a Opção B se for a primeira vez lendo esses arquivos e o escopo das mudanças não estiver claro. Opção A é preferível se o conteúdo do YAML for diretamente mapeável às constantes do ranker.

**Validação:** Após escolher a opção, documentar qual foi a decisão e por quê em um comentário no arquivo `SESSION_LOG.md` (seção de novas entradas no final).

---

### TAREFA 2.2 — Implementar os ramos HITL `veto`/`expand_scope` em `orchestrator.py`

**Arquivo alvo:** `src/orchestrator.py` → método `_apply_hitl_decision`

**Contexto:** Os ramos `exclude_source`/`veto` e `expand_scope`/`expand` atualmente só fazem `logger.info(...)` e não têm efeito algum. O sistema pergunta ao usuário via HITL se quer vetar uma fonte e o veto é ignorado silenciosamente.

**O que fazer:**

1. **Ramo `veto`/`exclude_source`:** Filtrar os resultados da fonte vetada de `context.ranked_results`. Além disso, registrar o veto no `feedback_store.py` como sinal negativo (source_name da fonte vetada, signal="veto"):
   ```python
   elif action in ("exclude_source", "veto"):
       source_to_exclude = data.get("source") if isinstance(data, dict) else str(data)
       if source_to_exclude:
           # Filtrar resultados da fonte vetada
           original_count = len(context.ranked_results)
           context.ranked_results = [
               r for r in context.ranked_results
               if getattr(r, "source", None) != source_to_exclude
           ]
           removed = original_count - len(context.ranked_results)
           logger.info(
               "HITL veto applied: removed %d results from source '%s'",
               removed, source_to_exclude,
           )
           # Registrar como sinal negativo no feedback_store (se disponível)
           if hasattr(self, "feedback_store") and self.feedback_store:
               try:
                   self.feedback_store.record(
                       user_id=getattr(context, "user_id", "anonymous"),
                       query=context.query,
                       result_id=f"hitl_veto:{source_to_exclude}",
                       signal="not_useful",
                       source_name=source_to_exclude,
                   )
               except Exception:
                   logger.debug("feedback_store.record failed silently during HITL veto", exc_info=True)
   ```

2. **Ramo `expand_scope`/`expand`:** Re-invocar `SourcePlanner` com escopo ampliado. O comportamento esperado é adicionar sources secundários ao plano e disparar uma busca suplementar:
   ```python
   elif action in ("expand_scope", "expand"):
       expand_hint = data.get("hint") if isinstance(data, dict) else str(data)
       logger.info("HITL expand_scope triggered with hint: %s", expand_hint)
       # Adicionar hint ao contexto para que stages subsequentes possam usar
       if not hasattr(context, "expand_hints"):
           context.expand_hints = []
       context.expand_hints.append(expand_hint)
       # TODO-FASE2: implementar re-execução do SearchStage com sources adicionais
       # Por ora, o hint é registrado e o pipeline continua com results atuais
       logger.warning(
           "HITL expand_scope: hint registered but full re-search not yet implemented. "
           "Context will use existing results."
       )
   ```
   > **Nota:** A re-execução completa do SearchStage dentro do HITL é complexa (requer refatoração do pipeline). Por ora, implementar o filtro de veto (que é o caso mais crítico) e registrar o expand_hint no contexto. O TODO marcado é explícito e intencional — não é um placeholder esquecido.

**Validação:**
```bash
python -m pytest tests/ -k "hitl or orchestrator" -v
```
Deve haver ao menos 1 teste novo cobrindo o comportamento de filtragem do veto.

---

### TAREFA 2.3 — Plugar BibTeX e RIS como formatos em `report_generator.py`

**Arquivos alvo:** `src/report_generator.py` e possivelmente `api/main.py` / `cli/main.py`

**Contexto:** `BibTeXExporter` e `RISExporter` existem, têm testes passando e estão exportados em `src/exporters/__init__.py`, mas nunca são chamados por `report_generator.py`.

**O que fazer:**

1. Abrir `src/report_generator.py` e localizar onde `PDFExporter`, `DOCXExporter` e `PPTXExporter` são importados e chamados.
2. Importar `BibTeXExporter` e `RISExporter` do mesmo pacote.
3. Adicionar `"bibtex"` e `"ris"` como valores válidos no enum/list de formatos suportados.
4. Adicionar os ramos correspondentes no dispatch de exportação:
   ```python
   elif format == "bibtex":
       return BibTeXExporter().export(report_data)
   elif format == "ris":
       return RISExporter().export(report_data)
   ```
   > Adapte conforme a assinatura real dos exporters (abra `src/exporters/bibtex_exporter.py` e `ris_exporter.py` antes de escrever).
5. Se `api/main.py` tiver um campo `format` enumerado na request de geração de relatório, adicionar `"bibtex"` e `"ris"` lá também.
6. Se `cli/main.py` tiver uma opção `--format`, adicionar os dois novos valores.

**Validação:**
```bash
python -m pytest tests/test_citation_exporters.py -v   # testes existentes devem continuar passando
python -m pytest tests/ -k "report_generator" -v       # sem regressões
```

---

### TAREFA 2.4 — Popular `config/misinformation_domains.yaml` com dados reais

**Arquivo alvo:** `config/misinformation_domains.yaml`

**Contexto:** O arquivo tem apenas 4 domínios de placeholder (`fake-tech-news.com`, etc.) que não resolvem para sites reais. O `MisinformationDetector` está funcionalmente ativo, mas cosmético.

**O que fazer:**
Substituir o conteúdo atual por uma lista inicial de domínios reais com baixa confiabilidade técnica. Use fontes públicas conhecidas (NewsGuard ratings, Media Bias/Fact Check, listas de fact-checkers reconhecidos) como referência para montar a lista.

Estrutura mínima a manter:
```yaml
# Lista de domínios de baixa confiabilidade para detecção de desinformação.
# Fonte: compilação de listas públicas de fact-checkers.
# Última atualização: YYYY-MM-DD
# Para adicionar um domínio: validar em NewsGuard ou MBFC antes de incluir.

- domain: "zerohedge.com"
  reason: "financial conspiracy theories"
  severity: "high"

- domain: "naturalnews.com"
  reason: "pseudoscience and health misinformation"
  severity: "critical"

# ... adicionar ao menos 20-30 domínios reais
```

> **Atenção:** Inclua domínios com histórico documentado e público de desinformação. Não inclua domínios apenas por serem "controversos" — o critério é publicação documentada de informação factualmente falsa por fact-checkers reconhecidos.

**Validação:**
```bash
python -c "
import yaml
with open('config/misinformation_domains.yaml') as f:
    data = yaml.safe_load(f)
print(f'Total de domínios: {len(data)}')
assert len(data) >= 20, 'Lista muito pequena'
print('ok')
"
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 2

- [ ] `config/scoring_weights.yaml` e `config/sources.yaml`: decisão documentada (conectar OU marcar como inativo) + `SESSION_LOG.md` atualizado
- [ ] `_apply_hitl_decision` no `orchestrator.py`: ramo veto filtra results + registra no feedback_store; ramo expand registra hint
- [ ] `python -m pytest tests/ -k "hitl or orchestrator" -v` → sem falhas, ao menos 1 novo teste para o veto
- [ ] `report_generator.py` aceita `format="bibtex"` e `format="ris"` sem `KeyError`
- [ ] `python -m pytest tests/test_citation_exporters.py -v` → todos os testes passando
- [ ] `config/misinformation_domains.yaml` tem ≥ 20 domínios reais
- [ ] `python -m pytest tests/ --tb=short -q` → suíte completa sem novas falhas

---

## 🚫 FORA DO ESCOPO DESTA FASE

- Autenticação/rate limiting da API REST → Fase 3
- `ResultID` canônico e `FeedbackRanker` → Fase 4
- `GenericAPISearcher` e novas fontes → Fase 6+
- `scheduler.py` integrado ao CLI/API → decisão de produto (não bug técnico)
