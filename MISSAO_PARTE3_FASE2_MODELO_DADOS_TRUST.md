# MISSÃO PARTE3 — FASE 2: Modelo de Dados + TrustRuleStore

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 2 do plano derivado de `PLANO_SRA_PARTE_3.md`.
> Pré-requisito: **Fase 1 concluída** (servidores unificados, subsistemas conectados).
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — POR QUE ESSA FASE VEM ANTES DAS FEATURES

O clustering (Fase 4) e a allowlist pessoal (Fase 5) dependem de campos que **não existem ainda** no `SearchResult`. Se esses campos não estiverem prontos, as fases seguintes não podem ser integradas sem quebrar o modelo de dados.

Esta fase é **100% aditiva** — novos campos com `default=None`/`default_factory=list`, sem remover nada existente. Isso garante que nenhum código atual quebra.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | Todo código Python novo ou modificado |
| `test-driven-development` | `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md` | Testes do TrustRuleStore |
| `clean-code` | `E:\Meus LLMs\.claude\skills\clean-code\SKILL.md` | Padrão mirror do FeedbackStore |

---

## 📋 TAREFAS (em ordem obrigatória)

### TAREFA 2.1 — Estender `SearchResult` em `src/types.py`

**Contexto (§7):** O `SearchResult` atual já tem `confidence_score`, `evidence_quality`, `citations`. Faltam os campos para clustering e allowlist.

**O que fazer:**

1. Abrir `src/types.py` e localizar a classe `SearchResult` (ou `SRAModel` — confirmar o nome real).
2. Adicionar os 3 campos novos, **todos com default** para não quebrar código existente:

```python
from typing import Literal
from pydantic import Field

class SearchResult(SRAModel):
    # ... campos existentes (não remova nenhum) ...

    cluster_id: str | None = None
    """Preenchido pelo passo de clustering (Fase 4) — resultados de fontes
    diferentes sobre o mesmo fato/evento compartilham o mesmo cluster_id.
    None = não passou por clustering ou não teve nenhum resultado similar."""

    corroborated_by: list[str] = Field(default_factory=list)
    """Lista de `source` que corroboram o mesmo cluster_id — permite ao
    SynthesizeStage/ReportStage mostrar 'confirmado por N fontes independentes'
    em vez de listar N itens redundantes."""

    trust_tier: Literal["allow", "neutral", "deny"] = "neutral"
    """Preenchido no SearchStage a partir da allowlist/denylist pessoal do usuário (Fase 5).
    'deny' pode ser filtrado completamente ou penalizado no score.
    Default 'neutral' garante backward compatibility total."""
```

3. Confirmar que `py_compile` e os testes existentes passam sem mudança (campos novos com default não quebram nada).

**Validação:**
```bash
python -m py_compile src/types.py
python -m pytest tests/ -k "search_result or types" -v
```

---

### TAREFA 2.2 — Criar `src/trust_rule_store.py`

**Contexto (§12.2):** Mirror deliberado do `FeedbackStore` — mesma arquitetura JSONL, mesma convenção de arquivo. O código completo está no plano.

**O que fazer:**

Criar `src/trust_rule_store.py` com o conteúdo abaixo (já especificado no plano, copiar e adaptar o caminho `_DEFAULT_PATH`):

```python
"""
TrustRuleStore — persiste regras de allowlist/denylist pessoal de fontes.

Cada registro é uma linha JSON com: user_id, source, tier, timestamp.
O arquivo padrão é reports/_trust_rules.jsonl, configurável via
TRUST_RULE_STORE_PATH. Espelha deliberadamente o padrão de
src/feedback_store.py para manter o projeto consistente.
"""

import json
import logging
import os
from datetime import datetime, UTC
from pathlib import Path

logger = logging.getLogger(__name__)

VALID_TIERS = {"allow", "deny"}

_DEFAULT_PATH = Path(__file__).parent.parent / "reports" / "_trust_rules.jsonl"


class TrustRuleStore:
    def __init__(self, store_path: str | None = None):
        self.path = Path(
            store_path or os.environ.get("TRUST_RULE_STORE_PATH", str(_DEFAULT_PATH))
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, user_id: str, source: str, tier: str) -> dict:
        """Grava (ou atualiza, via append + resolução no load) uma regra."""
        if not user_id:
            raise ValueError("user_id não pode ser vazio")
        if tier not in VALID_TIERS:
            raise ValueError(f"tier inválido: '{tier}'. Válidos: {sorted(VALID_TIERS)}")

        entry = {
            "user_id": user_id,
            "source": source,
            "tier": tier,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("Regra de confiança gravada: user=%s source=%s -> %s", user_id, source, tier)
        return entry

    def load_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Linha inválida ignorada: %s", line[:80])
        return records

    def get_rules_for_user(self, user_id: str) -> dict[str, str]:
        """Retorna {source: tier} com a regra MAIS RECENTE por fonte."""
        by_source: dict[str, tuple[str, str]] = {}
        for rec in self.load_all():
            if rec.get("user_id") != user_id:
                continue
            src, tier, ts = rec.get("source", ""), rec.get("tier", ""), rec.get("timestamp", "")
            if src and (src not in by_source or ts > by_source[src][0]):
                by_source[src] = (ts, tier)
        return {src: tier for src, (_, tier) in by_source.items()}

    def clear(self, user_id: str | None = None) -> int:
        """Remove regras. Se user_id for None, limpa tudo."""
        records = self.load_all()
        if user_id is None:
            if self.path.exists():
                self.path.unlink()
            return len(records)
        remaining = [r for r in records if r.get("user_id") != user_id]
        removed = len(records) - len(remaining)
        with open(self.path, "w", encoding="utf-8") as f:
            for r in remaining:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return removed
```

**Validação:**
```bash
python -m py_compile src/trust_rule_store.py
```

---

### TAREFA 2.3 — Escrever testes do `TrustRuleStore`

**Arquivo alvo:** `tests/test_trust_rule_store.py` (arquivo NOVO)

**O que fazer:**

```python
"""Testes unitários do TrustRuleStore."""
import pytest
from src.trust_rule_store import TrustRuleStore


class TestTrustRuleStore:

    def test_record_and_load(self, tmp_path):
        store = TrustRuleStore(store_path=str(tmp_path / "rules.jsonl"))
        entry = store.record(user_id="u1", source="reddit", tier="allow")
        assert entry["tier"] == "allow"
        assert len(store.load_all()) == 1

    def test_get_rules_latest_wins(self, tmp_path):
        store = TrustRuleStore(store_path=str(tmp_path / "rules.jsonl"))
        store.record(user_id="u1", source="reddit", tier="allow")
        store.record(user_id="u1", source="reddit", tier="deny")  # mais recente
        rules = store.get_rules_for_user("u1")
        assert rules["reddit"] == "deny"

    def test_user_isolation(self, tmp_path):
        store = TrustRuleStore(store_path=str(tmp_path / "rules.jsonl"))
        store.record(user_id="u1", source="reddit", tier="allow")
        store.record(user_id="u2", source="reddit", tier="deny")
        assert store.get_rules_for_user("u1")["reddit"] == "allow"
        assert store.get_rules_for_user("u2")["reddit"] == "deny"

    def test_invalid_tier_raises(self, tmp_path):
        store = TrustRuleStore(store_path=str(tmp_path / "rules.jsonl"))
        with pytest.raises(ValueError, match="tier inválido"):
            store.record(user_id="u1", source="reddit", tier="maybe")

    def test_empty_user_id_raises(self, tmp_path):
        store = TrustRuleStore(store_path=str(tmp_path / "rules.jsonl"))
        with pytest.raises(ValueError, match="user_id não pode ser vazio"):
            store.record(user_id="", source="reddit", tier="allow")

    def test_clear_by_user(self, tmp_path):
        store = TrustRuleStore(store_path=str(tmp_path / "rules.jsonl"))
        store.record(user_id="u1", source="reddit", tier="allow")
        store.record(user_id="u2", source="twitter", tier="deny")
        removed = store.clear(user_id="u1")
        assert removed == 1
        assert store.get_rules_for_user("u1") == {}
        assert "twitter" in store.get_rules_for_user("u2")
```

**Validação:**
```bash
python -m pytest tests/test_trust_rule_store.py -v
```

---

### TAREFA 2.4 — Integrar `TrustRuleStore` em `source_planner.py`

**Contexto (§12.3):** As regras de allowlist/denylist devem ser lidas em `SourcePlanner.plan()` via `context.extra["trust_rules"]` — usando o dicionário `extra` do `PipelineContext` (convenção já documentada no docstring do projeto).

**O que fazer:**

1. Abrir `src/source_planner.py` e localizar o método `plan()`.
2. Adicionar a lógica de filtragem **após** montar o plano estático/LLM:

```python
# Dentro de SourcePlanner.plan(), após montar o plano base:
trust_rules = context.extra.get("trust_rules", {})  # {source: "allow"|"deny"}
if trust_rules:
    denied = {s for s, tier in trust_rules.items() if tier == "deny"}
    allowed_priority = [s for s, tier in trust_rules.items() if tier == "allow"]

    base_plan.primary = [s for s in base_plan.primary if s not in denied]
    base_plan.secondary = [s for s in base_plan.secondary if s not in denied]
    # Promove fontes "allow" para o topo de primary:
    for s in reversed(allowed_priority):
        if s not in base_plan.primary:
            base_plan.primary.insert(0, s)
```

3. Abrir os 4 entry points (`api/main.py`, `cli/main.py`, `src/mcp_server.py`) e popular `context.extra["trust_rules"]` antes de chamar `orchestrator.research()`:

```python
# Em cada entry point, antes de chamar orchestrator.research():
from src.trust_rule_store import TrustRuleStore

trust_store = TrustRuleStore()  # usar instância compartilhada, não nova a cada request
user_id = getattr(request, "user_id", "anonymous")  # adapte ao mecanismo de auth existente
context.extra["trust_rules"] = trust_store.get_rules_for_user(user_id)
```

> **Nota sobre user_id:** Enquanto a autenticação real não existe (Parte 2 §9.1), usar `"anonymous"` como fallback é aceitável para desenvolvimento. **Não bloquear a implementação esperando um mecanismo de auth completo.**

**Validação:**
```bash
python -m py_compile src/source_planner.py
python -m pytest tests/ -k "source_planner or trust" -v
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 2

- [ ] `SearchResult` em `src/types.py` tem campos `cluster_id`, `corroborated_by`, `trust_tier`
- [ ] `py_compile src/types.py` sem erros, testes existentes passam sem mudança
- [ ] `src/trust_rule_store.py` criado, `py_compile` limpo
- [ ] `python -m pytest tests/test_trust_rule_store.py -v` → todos passam
- [ ] `SourcePlanner.plan()` aplica regras `allow`/`deny` de `context.extra["trust_rules"]`
- [ ] Entry points populam `context.extra["trust_rules"]` antes de chamar orchestrator
- [ ] `python -m pytest tests/ --tb=short -q` → zero novas regressões
- [ ] Commit com todos os arquivos desta fase

---

## 🚫 FORA DO ESCOPO DESTA FASE

- Clustering em si → Fase 4 (usa `cluster_id`/`corroborated_by` criados aqui)
- UI de allowlist no Streamlit → Fase 5 (usa `TrustRuleStore` criado aqui)
- `GenericAPISearcher` → Fase 3
- Integração do `trust_tier` no `ScoreStage` → Fase 5
