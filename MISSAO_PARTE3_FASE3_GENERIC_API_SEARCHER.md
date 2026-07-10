# MISSÃO PARTE3 — FASE 3: GenericAPISearcher + Fontes Verticais

> **LEIA ESTE ARQUIVO INTEIRO ANTES DE ESCREVER QUALQUER CÓDIGO.**
> Esta é a Fase 3 do plano derivado de `PLANO_SRA_PARTE_3.md`.
> Pré-requisito: **Fases 1 e 2 concluídas** (arquitetura limpa, modelo de dados correto).
> Execute SOMENTE o que está descrito aqui.

---

## ⚠️ CONTEXTO — A MAIOR ALAVANCAGEM DO PLANO PARTE 3

O `GenericAPISearcher` permite adicionar **novas fontes sem escrever Python novo** — apenas editando um arquivo YAML. É a peça que transforma o SRA de "lista de integrações nomeadas" para um motor extensível de verdade.

**Limitações conhecidas e deliberadas (NÃO implementar além disso):**
- Paginação: fora do escopo v1 — cada fonte devolve só a primeira página
- Auth exótica (OAuth2, SigV4): não coberta — só `none`, `header_api_key`, `bearer_token`, `query_api_key`
- Transforms não-triviais via YAML: **não** implementar `eval()` — segurança
- `GenericWebsiteSearcher` (§2 do plano): **fora do escopo desta fase** — requer decisão de produto sobre governance de crawling

---

## 🛠️ SKILLS A USAR

| Skill | Caminho | Quando usar |
|---|---|---|
| `python-pro` | `E:\Meus LLMs\.claude\skills\python-pro\SKILL.md` | Classe `GenericAPISearcher` |
| `http-request-mastery` | `E:\Meus LLMs\.claude\skills\http-request-mastery\SKILL.md` | Chamadas HTTP resilientes, rate limiting |
| `test-driven-development` | `E:\Meus LLMs\.claude\skills\test-driven-development\SKILL.md` | Testes de conformidade com fixtures |

---

## 📋 TAREFAS (em ordem obrigatória)

### TAREFA 3.1 — Criar `src/search/generic_api_searcher.py`

**Contexto (§1, §13.1):** Herda de `APISearcher` (não de `BaseSearcher` puro) para reaproveitar rate limiting/cache/circuit breaker já prontos.

**O que fazer:**

1. Abrir `src/search/api_searcher.py` e confirmar a assinatura do construtor e os métodos disponíveis (`_make_request`, `_http_request`, etc.).
2. Criar `src/search/generic_api_searcher.py`:

```python
"""
GenericAPISearcher — Searcher configurável via config/generic_sources.yaml.

Uma instância é criada pelo SearcherFactory por cada entrada habilitada no YAML.
O campo `source` do SearchResult retornado usa o `id` da entrada, tornando
essas fontes indistinguíveis de searchers escritos à mão para o resto do pipeline.
"""
from __future__ import annotations

import logging
from typing import Any

from src.search.api_searcher import APISearcher, APISearcherConfig
from src.types import SearchResult  # ajuste o import conforme o caminho real

logger = logging.getLogger(__name__)


class GenericAPISearcher(APISearcher):
    """Searcher configurável via YAML — uma instância por entrada habilitada."""

    def __init__(self, source_config: dict, http_config: APISearcherConfig | None = None):
        super().__init__(config=http_config or APISearcherConfig(base_url=source_config["base_url"]))
        self.source_config = source_config
        self.source_id = source_config["id"]

    async def search(self, query: str, **kwargs) -> list[SearchResult]:
        cfg = self.source_config
        try:
            if cfg.get("lookup_only"):
                params = {cfg["query_param"]: query}
            else:
                params = {**cfg.get("static_params", {}), cfg["query_param"]: query}

            data = await self._make_request("GET", "", params=params)
            if not data:
                return []

            result_path = cfg.get("result_path")
            items = self._resolve_path(data, result_path) if result_path else [data]
            if not isinstance(items, list):
                logger.warning("%s: result_path '%s' não resolveu para lista", self.source_id, result_path)
                return []

            return [r for r in (self._normalize(item) for item in items) if r is not None]

        except Exception as exc:
            logger.warning("%s: busca falhou: %s", self.source_id, exc)
            return []

    def _normalize(self, item: dict) -> SearchResult | None:
        try:
            mapping = self.source_config["mapping"]
            title = self._resolve_path(item, mapping.get("title", ""), default="")
            description = self._resolve_path(item, mapping.get("description", ""), default="")
            url = self._render_url(mapping.get("url_template"), item)
            metrics = {
                k: self._resolve_path(item, v, default=None)
                for k, v in mapping.get("metrics", {}).items()
            }
            # strip_html simples para campos que podem conter HTML (ex: Wikipedia snippet)
            if description:
                import re
                description = re.sub(r"<[^>]+>", "", str(description))

            return SearchResult(
                source=self.source_id,
                title=str(title) if title else "",
                url=str(url) if url else "",
                description=str(description) if description else "",
                metrics={k: v for k, v in metrics.items() if v is not None},
                raw=item,
            )
        except Exception as exc:
            logger.debug("%s: normalize falhou para item: %s", self.source_id, exc)
            return None

    @staticmethod
    def _resolve_path(data: Any, dot_path: str, default: Any = None) -> Any:
        """Resolve 'query.search' ou 'first_sentence.0' (índice de lista)."""
        if not dot_path:
            return default
        node = data
        for part in dot_path.split("."):
            if isinstance(node, list):
                if part.isdigit() and int(part) < len(node):
                    node = node[int(part)]
                else:
                    return default
            elif isinstance(node, dict):
                node = node.get(part, default)
            else:
                return default
            if node is default:
                return default
        return node

    @staticmethod
    def _render_url(template: str | None, item: dict) -> str:
        """Renderiza url_template substituindo {campo} pelo valor do item."""
        if not template:
            return ""
        try:
            # Substituição simples: {campo} → item["campo"]
            import re
            def replacer(match):
                path = match.group(1)
                val = GenericAPISearcher._resolve_path(item, path, default="")
                from urllib.parse import quote
                return quote(str(val), safe="") if val else ""
            return re.sub(r"\{([^}]+)\}", replacer, template)
        except Exception:
            return ""
```

**Validação:**
```bash
python -m py_compile src/search/generic_api_searcher.py
```

---

### TAREFA 3.2 — Criar `config/generic_sources.yaml` com as 8 fontes reais

**Contexto (§1.1, §6, §11):** Todas as 8 fontes só precisam de `httpx` (já declarado na Fase 5 da Parte 2). Nenhuma dependência nova.

**O que fazer:**

Criar `config/generic_sources.yaml` com o conteúdo abaixo. **ATENÇÃO:** verificar se o arquivo já existe (a Fase 6 da Parte 2 pode ter criado uma versão — se sim, ABRIR antes de sobrescrever e fundir o conteúdo):

```yaml
# config/generic_sources.yaml
# Fontes configuradas declarativamente para o GenericAPISearcher.
# Para adicionar uma fonte nova: acrescente uma entrada abaixo e crie a fixture
# em tests/fixtures/generic_sources/{id}.json (obrigatório para o CI passar).
# Não use eval() nem código arbitrário nos campos — segurança.

sources:
  - id: "wikipedia"
    enabled: true
    base_url: "https://pt.wikipedia.org/w/api.php"
    method: "GET"
    static_params:
      action: "query"
      list: "search"
      format: "json"
      srlimit: "10"
    query_param: "srsearch"
    result_path: "query.search"
    mapping:
      title: "title"
      url_template: "https://pt.wikipedia.org/wiki/{title}"
      description: "snippet"
    requires_api_key: false
    rate_limit_per_minute: 200

  - id: "open_library"
    enabled: true
    base_url: "https://openlibrary.org/search.json"
    method: "GET"
    query_param: "q"
    result_path: "docs"
    mapping:
      title: "title"
      url_template: "https://openlibrary.org{key}"
      description: "subtitle"
      metrics:
        first_publish_year: "first_publish_year"
        edition_count: "edition_count"
    requires_api_key: false

  - id: "npm_registry"
    enabled: true
    base_url: "https://registry.npmjs.org/-/v1/search"
    method: "GET"
    query_param: "text"
    result_path: "objects"
    mapping:
      title: "package.name"
      url_template: "https://www.npmjs.com/package/{package.name}"
      description: "package.description"
      metrics:
        score_final: "score.final"
    requires_api_key: false

  - id: "pypi"
    enabled: true
    base_url: "https://pypi.org/search/"
    method: "GET"
    query_param: "q"
    # PyPI JSON API não tem busca por texto livre — usando endpoint HTML/search.
    # Limitação conhecida: resultado é HTML, não JSON. Requer parse especial.
    # NOTA: desabilitar se o parse HTML não for implementado nesta fase.
    # lookup_only: true  # descomente se só quiser lookup por nome exato via /pypi/{name}/json
    result_path: null
    mapping:
      title: "name"
      url_template: "https://pypi.org/project/{name}/"
      description: "summary"
    requires_api_key: false

  - id: "dictionary"
    enabled: true
    base_url: "https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
    lookup_only: true
    query_param: "word"
    result_path: null
    mapping:
      title: "0.word"
      description: "0.meanings.0.definitions.0.definition"
      url_template: null
    requires_api_key: false

  - id: "musicbrainz"
    enabled: true
    base_url: "https://musicbrainz.org/ws/2/recording"
    method: "GET"
    query_param: "query"
    static_params:
      fmt: "json"
    result_path: "recordings"
    mapping:
      title: "title"
      url_template: "https://musicbrainz.org/recording/{id}"
      description: "artist-credit.0.name"
    requires_api_key: false
    rate_limit_per_minute: 50   # MusicBrainz é estrito — RESPEITAR

  - id: "domain_whois"
    enabled: true
    base_url: "https://rdap.org/domain/{domain}"
    lookup_only: true
    query_param: "domain"
    mapping:
      title: "ldhName"
      description: "status.0"
      url_template: null
    requires_api_key: false
    # Uso legítimo: verificar credibilidade de uma fonte desconhecida (due diligence).
    # NÃO usar para rastreamento de pessoas.

  - id: "open_meteo"
    enabled: false   # desabilitado por padrão — requer lat/lon, não query de texto livre
    base_url: "https://api.open-meteo.com/v1/forecast"
    lookup_only: true
    static_params:
      current_weather: "true"
    query_param: "latitude"   # a ser expandido com longitude separado
    mapping:
      title: "static:Previsão do tempo"
      description: "current_weather.temperature"
      url_template: null
    requires_api_key: false
```

---

### TAREFA 3.3 — Criar fixtures de payload para o teste de conformidade

**Contexto (§13.2):** Todo item novo em `generic_sources.yaml` precisa de uma fixture em `tests/fixtures/generic_sources/{id}.json` com um payload real de exemplo.

**O que fazer:**

Criar o diretório `tests/fixtures/generic_sources/` e fazer chamadas reais a cada API para capturar um exemplo de resposta. Salvar como JSON:

```bash
# Exemplo para Wikipedia (rodar uma vez para capturar fixture):
python -c "
import httpx, json, pathlib
r = httpx.get('https://pt.wikipedia.org/w/api.php', params={'action':'query','list':'search','format':'json','srlimit':'3','srsearch':'inteligencia artificial'})
pathlib.Path('tests/fixtures/generic_sources').mkdir(parents=True, exist_ok=True)
pathlib.Path('tests/fixtures/generic_sources/wikipedia.json').write_text(json.dumps(r.json(), ensure_ascii=False, indent=2))
print('ok')
"
```

Repetir para cada fonte habilitada. Para fontes que não retornam JSON puro, adaptar o script.

---

### TAREFA 3.4 — Criar `tests/test_generic_api_searcher_conformance.py`

**Contexto (§13.2):** Teste de conformidade que valida cada fonte do YAML contra sua fixture. É o equivalente do "teste de reachability" da Parte 2 — garante que nenhum YAML mal escrito falhe silenciosamente.

```python
"""
tests/test_generic_api_searcher_conformance.py

Para toda entrada habilitada em config/generic_sources.yaml, confirma que:
  1. result_path resolve para lista não-vazia no payload de fixture
  2. Todo campo em `mapping` resolve sem erro no primeiro item
"""
import json
import pytest
import yaml
from pathlib import Path
from src.search.generic_api_searcher import GenericAPISearcher

CONFIG_PATH = Path("config/generic_sources.yaml")
FIXTURES_DIR = Path("tests/fixtures/generic_sources")


def _load_enabled_sources() -> list[dict]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        all_sources = yaml.safe_load(f).get("sources", [])
    return [s for s in all_sources if s.get("enabled", True)]


@pytest.mark.parametrize("source_cfg", _load_enabled_sources(), ids=lambda c: c["id"])
def test_source_conformance(source_cfg):
    fixture_path = FIXTURES_DIR / f"{source_cfg['id']}.json"
    assert fixture_path.exists(), (
        f"Fonte '{source_cfg['id']}' não tem fixture em {fixture_path}. "
        f"Crie uma com uma chamada real à API."
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    result_path = source_cfg.get("result_path")
    if result_path:
        items = GenericAPISearcher._resolve_path(payload, result_path)
        assert items and isinstance(items, list), (
            f"result_path '{result_path}' da fonte '{source_cfg['id']}' "
            f"não resolveu para lista não-vazia no fixture."
        )
        first = items[0]
    else:
        first = payload if isinstance(payload, dict) else (payload[0] if isinstance(payload, list) and payload else {})

    mapping = source_cfg.get("mapping", {})
    for field_name in ("title", "description"):
        path = mapping.get(field_name)
        if path and not path.startswith("static:"):
            value = GenericAPISearcher._resolve_path(first, path)
            assert value is not None, (
                f"Campo '{field_name}' (path='{path}') da fonte '{source_cfg['id']}' "
                f"resolveu para None no fixture — mapping provavelmente errado."
            )
```

**Validação:**
```bash
python -m pytest tests/test_generic_api_searcher_conformance.py -v
```

---

### TAREFA 3.5 — Registrar `GenericAPISearcher` no `SearcherFactory`

**O que fazer:**

1. Abrir `src/search/factory.py` e localizar onde os searchers são registrados/instanciados.
2. Adicionar lógica para carregar `config/generic_sources.yaml` e instanciar `GenericAPISearcher` por entrada habilitada:

```python
# Em SearcherFactory.__init__() ou _build_searchers():
import yaml
from pathlib import Path
from src.search.generic_api_searcher import GenericAPISearcher

generic_config_path = Path("config/generic_sources.yaml")
if generic_config_path.exists():
    with open(generic_config_path, encoding="utf-8") as f:
        generic_sources = yaml.safe_load(f).get("sources", [])
    for source_cfg in generic_sources:
        if source_cfg.get("enabled", True):
            searcher = GenericAPISearcher(source_config=source_cfg)
            self._register(source_cfg["id"], searcher)
            logger.debug("GenericAPISearcher registrado: %s", source_cfg["id"])
```

3. Confirmar que `domains.yaml` pode referenciar os IDs das fontes genéricas (ex: `"wikipedia"`, `"open_library"`) sem código adicional — devem funcionar como qualquer outra fonte.

**Validação:**
```bash
python -m pytest tests/ -k "factory or searcher" -v
python -m pytest tests/test_generic_api_searcher_conformance.py -v
python -m pytest tests/ --tb=short -q
```

---

## ✅ CRITÉRIO DE CONCLUSÃO DA FASE 3

- [ ] `src/search/generic_api_searcher.py` criado, `py_compile` limpo
- [ ] `config/generic_sources.yaml` criado com 8 fontes (open_meteo desabilitado por padrão)
- [ ] `tests/fixtures/generic_sources/{id}.json` existem para todas as fontes habilitadas
- [ ] `python -m pytest tests/test_generic_api_searcher_conformance.py -v` → todos passam
- [ ] `SearcherFactory` instancia automaticamente os searchers genéricos ao iniciar
- [ ] `python -m pytest tests/ --tb=short -q` → zero novas regressões
- [ ] Limitações conhecidas documentadas em `config/generic_sources.yaml` e/ou `EXPERIMENTAL_MODULES.md`
- [ ] Commit com todos os arquivos desta fase

---

## 🚫 FORA DO ESCOPO DESTA FASE

- `GenericWebsiteSearcher` → decisão de produto pendente
- Paginação → limitação conhecida v1, documentar e não implementar
- Transforms não-triviais via YAML → `eval()` proibido por segurança
- Clustering → Fase 4
- UI de allowlist → Fase 5
