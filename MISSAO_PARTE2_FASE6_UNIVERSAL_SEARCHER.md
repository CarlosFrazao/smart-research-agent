# MISSÃO PARTE2 — FASE 6: Canivete Suíço Universal (GenericAPISearcher + Fontes Verticais)

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 6 do plano derivado da `AUDITORIA_SRA_PARTE_2.md`.
> Pré-requisito: **Fases 1 a 5 concluídas** — especialmente o teste de wiring da Fase 1,
> que vai garantir que tudo que você criar aqui seja automaticamente detectado se ficar desconectado.
> Execute SOMENTE o que está descrito aqui.

---

## 🎯 OBJETIVO DA FASE

Transformar o SRA de "lista de integrações nomeadas" para um **canivete suíço universal real** — onde adicionar uma nova fonte é uma questão de YAML, não de código Python.

Esta fase cria a infraestrutura que **multiplica** o valor de todas as fases anteriores.

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `.claude/skills/python-pro/SKILL.md` | Para `GenericAPISearcher` e `GenericWebsiteSearcher` |
| `api-patterns` | `.claude/skills/api-patterns/SKILL.md` | Para o design do catálogo YAML e o MCP tool `search_anything` |
| `test-driven-development` | `.claude/skills/test-driven-development/SKILL.md` | Para testes do GenericAPISearcher |
| `clean-code` | `.claude/skills/clean-code/SKILL.md` | Para revisão do código dos adaptadores genéricos |

---

## 📋 TAREFAS (em ordem recomendada)

### TAREFA 6.1 — Implementar `GenericAPISearcher`

**Arquivo alvo:** `src/search/generic_api_searcher.py` (arquivo NOVO)

**Contexto:** Esta é a peça mais importante de toda a auditoria para o objetivo de "pesquisar qualquer coisa". Em vez de uma classe Python por fonte, uma única classe lê a definição da fonte a partir de um catálogo YAML.

**O que fazer:**

1. Criar `config/generic_sources.yaml` — o catálogo de fontes genéricas:
```yaml
# Catálogo de fontes de busca configuráveis.
# Cada entrada vira uma fonte disponível sem escrever código Python novo.
# Campos obrigatórios: id, base_url, query_param, result_path
# Campos opcionais: title_field, url_template, snippet_field, max_results, timeout

sources:
  - id: "open_library"
    name: "Open Library (Internet Archive)"
    base_url: "https://openlibrary.org/search.json"
    query_param: "q"
    result_path: "docs"           # JSONPath: onde está a lista de resultados
    title_field: "title"
    url_template: "https://openlibrary.org{key}"
    snippet_field: "first_sentence.value"
    max_results: 10
    timeout: 15

  - id: "core_ac_uk"
    name: "CORE.ac.uk (Open Access Research)"
    base_url: "https://api.core.ac.uk/v3/search/works"
    query_param: "q"
    result_path: "results"
    title_field: "title"
    url_template: "{downloadUrl}"
    snippet_field: "abstract"
    max_results: 10
    timeout: 20
    headers:
      Authorization: "Bearer {CORE_API_KEY}"  # variável de ambiente

  - id: "doaj"
    name: "Directory of Open Access Journals"
    base_url: "https://doaj.org/api/search/articles/{query}"
    query_param: null   # query na URL diretamente
    result_path: "results"
    title_field: "bibjson.title"
    url_template: "https://doaj.org/article/{id}"
    snippet_field: "bibjson.abstract"
    max_results: 10
    timeout: 15

  - id: "osm_nominatim"
    name: "OpenStreetMap Nominatim (Geocoding)"
    base_url: "https://nominatim.openstreetmap.org/search"
    query_param: "q"
    result_path: null   # a resposta é diretamente uma lista
    title_field: "display_name"
    url_template: "https://www.openstreetmap.org/#map=15/{lat}/{lon}"
    snippet_field: "type"
    max_results: 5
    timeout: 10
    extra_params:
      format: "json"
      addressdetails: "1"

  # Adicionar mais fontes aqui — sem escrever código Python
```

2. Criar `src/search/generic_api_searcher.py`:
```python
"""
GenericAPISearcher — adaptador configurável que transforma qualquer API REST pública
em fonte de busca do SRA, sem escrever classe Python nova.

Configurado via config/generic_sources.yaml.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
import yaml
from jmespath import search as jmespath_search  # pip: jmespath

from src.search.base_searcher import BaseSearcher  # ajuste o import conforme o projeto
from src.models import SearchResult              # ajuste o import

logger = logging.getLogger(__name__)

_SOURCE_DEF_CACHE: dict[str, dict] = {}


def _load_source_def(source_id: str) -> dict | None:
    """Carrega a definição de uma fonte do catálogo YAML (com cache)."""
    global _SOURCE_DEF_CACHE
    if not _SOURCE_DEF_CACHE:
        catalog_path = os.path.join(os.path.dirname(__file__), "../../config/generic_sources.yaml")
        with open(catalog_path) as f:
            data = yaml.safe_load(f)
        _SOURCE_DEF_CACHE = {s["id"]: s for s in data.get("sources", [])}
    return _SOURCE_DEF_CACHE.get(source_id)


def _resolve_value(obj: Any, field_path: str) -> str:
    """Resolve um campo usando notação de ponto (ex: 'bibjson.title')."""
    parts = field_path.split(".")
    current = obj
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part, "")
        else:
            return ""
    return str(current) if current else ""


class GenericAPISearcher(BaseSearcher):
    """
    Searcher genérico configurado por YAML.
    Uma instância por source_id declarado em config/generic_sources.yaml.
    """

    def __init__(self, source_id: str, timeout: int = 15):
        self.source_id = source_id
        self.source_def = _load_source_def(source_id)
        if not self.source_def:
            raise ValueError(f"Source '{source_id}' not found in generic_sources.yaml")
        self._timeout = self.source_def.get("timeout", timeout)
        self.enabled = True

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Executa a busca na API configurada e retorna resultados normalizados."""
        defn = self.source_def
        url = defn["base_url"]
        params: dict[str, Any] = dict(defn.get("extra_params", {}))

        if defn.get("query_param"):
            params[defn["query_param"]] = query
        else:
            url = url.replace("{query}", query)

        headers = {}
        for k, v in defn.get("headers", {}).items():
            # Resolver variáveis de ambiente nos headers (ex: {CORE_API_KEY})
            resolved = re.sub(r"\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), v)
            headers[k] = resolved

        params["limit"] = defn.get("max_results", max_results)

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("GenericAPISearcher[%s] failed: %s", self.source_id, e)
            return []

        # Extrair lista de resultados via result_path
        result_list = data
        if defn.get("result_path"):
            result_list = jmespath_search(defn["result_path"], data) or []

        results = []
        for item in result_list[: max_results]:
            title = _resolve_value(item, defn.get("title_field", "title"))
            snippet = _resolve_value(item, defn.get("snippet_field", ""))

            # Construir URL do resultado
            url_template = defn.get("url_template", "")
            item_url = re.sub(r"\{(\w+)\}", lambda m: str(item.get(m.group(1), "")), url_template)

            results.append(
                SearchResult(
                    title=title,
                    url=item_url,
                    snippet=snippet,
                    source=self.source_id,
                )
            )

        return results

    async def close(self) -> None:
        pass  # httpx AsyncClient é criado/fechado por request — sem estado persistente
```

3. Registrar fontes genéricas no `SearcherFactory`:
```python
# Em src/search/factory.py, após os outros searchers:
from src.search.generic_api_searcher import GenericAPISearcher, _load_source_def
import yaml

# Registrar todas as fontes do catálogo generic_sources.yaml
try:
    with open("config/generic_sources.yaml") as f:
        catalog = yaml.safe_load(f)
    for source_def in catalog.get("sources", []):
        source_id = source_def["id"]
        if source_id not in searchers:  # não sobrescrever searchers dedicados
            searchers[source_id] = GenericAPISearcher(source_id)
except Exception as e:
    logger.warning("Could not load generic_sources.yaml: %s", e)
```

**Validação:** O teste de wiring da Fase 1 deve passar automaticamente para as novas fontes genéricas, pois elas serão registradas no SearcherFactory. Se `open_library`, `core_ac_uk`, etc. estiverem em `DOMAIN_SOURCES` (ou forem adicionadas), o teste vai validar o wiring automaticamente.

---

### TAREFA 6.2 — Adicionar novas fontes ao `DOMAIN_SOURCES` e `domains.yaml`

**Arquivos alvo:** `src/source_planner.py` (constante `DOMAIN_SOURCES`) e `config/domains.yaml`

**O que fazer:**
Adicionar as fontes genéricas nos domínios apropriados como fontes **secundárias** (não primárias — não substituem as fontes existentes, complementam):

```python
# Em DOMAIN_SOURCES, adicionar nas listas secondary:
"academic": {
    "primary": ["arxiv", "pubmed", ...],
    "secondary": [..., "open_library", "core_ac_uk", "doaj"],
},
"general": {
    "primary": [...],
    "secondary": [..., "open_library"],
},
"infrastructure": {
    "primary": [...],
    "secondary": [..., "osm_nominatim"],
},
```

---

### TAREFA 6.3 — Implementar MCP tool `search_anything`

**Arquivo alvo:** `src/mcp_server.py`

**Contexto:** O SRA tem 15 tools MCP, mas nenhuma é um "modo canivete suíço" que força o roteamento pelo domínio `universal`/`open_web`. Esta tool expõe isso de forma simples para outros agentes.

**O que fazer:**
```python
@mcp.tool()
async def search_anything(
    query: str,
    hint_domain: str | None = None,
    max_results: int = 10,
) -> dict:
    """
    Pesquisa universal: usa o domínio 'universal' ou 'general' por padrão,
    cobrindo todas as fontes disponíveis sem precisar conhecer a taxonomia interna.

    Args:
        query: A consulta de pesquisa em linguagem natural.
        hint_domain: Dica de domínio opcional (ex: 'academic', 'dev_tools').
                     Se não informado, usa 'general' como fallback universal.
        max_results: Número máximo de resultados por fonte.

    Returns:
        Resultados de pesquisa multi-fonte normalizados.
    """
    domain = hint_domain or "general"
    # Usar o mesmo pipeline do research normal, mas forçando o domínio
    orchestrator = get_orchestrator()  # ajuste conforme como o orchestrator é obtido
    result = await orchestrator.research(query=query, domain=domain, max_results=max_results)
    return result.to_dict() if hasattr(result, "to_dict") else {"results": str(result)}
```

---

### TAREFA 6.4 — Promover `scheduler.py` a funcionalidade de primeira classe (monitoramento contínuo)

**Arquivos alvo:** `cli/main.py` e/ou `api/main.py`

**Contexto:** `scheduler.py` é um módulo completo de pesquisa recorrente + alertas via webhook, mas só é alcançável pelo `src/main.py` legado, não documentado no README. Para o objetivo de "vigiar este tópico", precisa ser promovido.

**O que fazer:**

1. Adicionar comandos Typer em `cli/main.py`:
```python
@app.command("schedule")
def schedule_research(
    query: str = typer.Argument(..., help="Query de pesquisa recorrente"),
    cron: str = typer.Option("0 8 * * *", help="Expressão cron (padrão: diário às 8h)"),
    webhook_url: str | None = typer.Option(None, help="URL para alertas de mudança"),
    domain: str = typer.Option("general", help="Domínio de pesquisa"),
):
    """Agenda uma pesquisa recorrente com detecção de mudanças e alertas."""
    scheduler = ResearchScheduler()
    job_id = scheduler.add_job(query=query, cron=cron, webhook_url=webhook_url, domain=domain)
    typer.echo(f"✅ Pesquisa agendada: {job_id}")
```

2. Adicionar endpoint REST em `api/main.py`:
```python
@app.post("/api/schedule", dependencies=[Depends(verify_api_key)])
async def schedule_research(payload: ScheduleRequest) -> dict:
    """Agenda uma pesquisa recorrente com alertas de mudança."""
    ...
```

3. Atualizar o `README.md` com exemplos de uso do agendamento.

---

### TAREFA 6.5 — Parser de operadores de busca avançada (`site:`, `filetype:`, `intitle:`)

**Arquivo alvo:** `src/pipeline/stages/expand_stage.py`

**Contexto:** Para ser um canivete suíço real, o usuário deve poder escrever `site:reddit.com melhor teclado mecânico` e o sistema traduzir isso automaticamente para a sintaxe equivalente de cada fonte.

**O que fazer:**

1. Criar `src/query_parser.py` com um parser de operadores:
```python
import re
from dataclasses import dataclass, field

@dataclass
class ParsedQuery:
    raw: str
    text: str                        # query sem os operadores
    site_filter: str | None = None   # site:reddit.com → "reddit.com"
    filetype: str | None = None      # filetype:pdf → "pdf"
    intitle: str | None = None       # intitle:python → "python"
    extra_operators: dict = field(default_factory=dict)

def parse_advanced_query(raw_query: str) -> ParsedQuery:
    """
    Parseia operadores de busca avançada da query do usuário.

    Exemplos:
    - "site:reddit.com best keyboard" → site_filter="reddit.com", text="best keyboard"
    - "filetype:pdf machine learning" → filetype="pdf", text="machine learning"
    """
    query = raw_query
    result = ParsedQuery(raw=raw_query, text=raw_query)

    site_match = re.search(r"\bsite:(\S+)", query)
    if site_match:
        result.site_filter = site_match.group(1)
        query = query.replace(site_match.group(0), "").strip()

    filetype_match = re.search(r"\bfiletype:(\S+)", query)
    if filetype_match:
        result.filetype = filetype_match.group(1)
        query = query.replace(filetype_match.group(0), "").strip()

    intitle_match = re.search(r"\bintitle:(\S+)", query)
    if intitle_match:
        result.intitle = intitle_match.group(1)
        query = query.replace(intitle_match.group(0), "").strip()

    result.text = query
    return result
```

2. Integrar `parse_advanced_query` em `expand_stage.py` para que os operadores sejam extraídos antes da expansão e propagados para os searchers que os suportam (SearXNG, DuckDuckGo, etc. suportam `site:` nativo).

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 6

- [ ] `config/generic_sources.yaml` criado com ao menos 4 fontes válidas
- [ ] `GenericAPISearcher` implementado e registrado no `SearcherFactory`
- [ ] Fontes genéricas passam no **teste de wiring** da Fase 1 automaticamente
- [ ] MCP tool `search_anything` funcional e testável via `mcp inspect`
- [ ] `scheduler.py` acessível via `cli/main.py schedule` e/ou `api/main.py /api/schedule`
- [ ] `parse_advanced_query()` implementado e com testes unitários
- [ ] `python -m pytest tests/ --tb=short -q` → suíte completa sem novas falhas
- [ ] README atualizado com seção "Adicionando novas fontes via YAML"

---

## 🔭 PRÓXIMAS FASES (além do escopo deste documento)

As ideias abaixo são registradas para referência futura, mas **não pertencem a nenhuma fase atual**:

- **Busca a partir de imagem/documento** (§13.3): TinEye/Google Lens API para busca reversa; extração de entidades de PDF/DOCX como seed de pesquisa
- **`GenericWebsiteSearcher`** (§13.1): crawler via sitemap.xml + índice ChromaDB local para qualquer site sem API pública
- **`WolframAlphaSearcher`** (§13.2): resposta computacional para perguntas de cálculo/fato direto
- **Paridade completa UI Streamlit ↔ API REST** (§6.4, §8.3): controles de HITL, agendamento e BibTeX/RIS na UI
- **Fontes verticais adicionais** (§13.5): ORCID, RemoteOK, HathiTrust, rastreamento de voos
