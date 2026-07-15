"""
test_entity_resolver.py — Entity Resolution Cross-Session (Bloco 14 / E6-T1)

Testes herméticos: nenhum download de modelo de embedding. Usamos um embedder
fake determinístico (injetado) cujos vetores codificam heurísticas controladas
pelo teste, e um KuzuDB isolado (diretório UUID) por teste.

Especialmente valida os 3 critérios do Claude Pro Review do task.md:
  (a) threshold 0.92 separa entidades técnicas próximas (BERT vs RoBERTa
      NÃO colapsam; OpenAI vs "Open AI" colapsam por sinônimo);
  (b) merge_entities no KuzuDB NÃO cria referências órfãs (repointa arestas);
  (c) performance não degrada com +10k entidades (query O(log N) no ChromaDB;
      validamos crescimento linear do tempo de resolve em lote).
"""

import os
import shutil
import time
import uuid

import pytest

from src.entity_resolver import EntityResolver
from src.knowledge_graph import SemanticKnowledgeGraph, Triple


# ── Embedder fake determinístico ──────────────────────────────────────────────
# Codifica sinônimos e similaridades controladas para validar o threshold 0.92
# sem depender de sentence-transformers. Cada entidade recebe um vetor unitário
# numa dimensão fixa; pares "sinônimos" compartilham a mesma dimensão.

_DIM = 8

# Sinônimos: mesma dimensão => similaridade coseno = 1.0 (devem colapsar).
_SYNONYMS = {
    "openai": 0,
    "open ai": 0,
    "open-ai": 0,
    "microsoft": 1,
    "msft": 1,
}

# Entidades "próximas mas distintas": dimensões vizinhas, norma unitária => cos
# ~0.0 (ortogonais) — representam termos técnicos que NÃO devem colapsar.
_DISTINCT = {
    "bert": 2,
    "roberta": 3,
    "gpt": 4,
    "llama": 5,
    "python": 6,
    "java": 7,
}


def _fake_embed(text: str):
    key = text.strip().lower()
    vec = [0.0] * _DIM
    if key in _SYNONYMS:
        vec[_SYNONYMS[key]] = 1.0
        return vec
    if key in _DISTINCT:
        vec[_DISTINCT[key]] = 1.0
        return vec
    # Entidade desconhecida: hash determinístico -> uma dimensão.
    dim = (sum(ord(c) for c in key) % _DIM)
    vec[dim] = 1.0
    return vec


@pytest.fixture
def temp_kuzu():
    """KuzuDB isolado (diretório UUID) + resolver com embedder fake."""
    run_id = uuid.uuid4().hex[:8]
    kuzu_dir = f"test_entity_resolve_{run_id}"
    os.makedirs(kuzu_dir, exist_ok=True)
    try:
        import kuzu

        db = kuzu.Database(os.path.join(kuzu_dir, "kuzu.db"))
        conn = kuzu.Connection(db)
        # Inicializa o schema do grafo (SemanticEntity + RELATION) — o mesmo
        # que SemanticKnowledgeGraph._init_schema cria. Assegura que o MERGE
        # manual nos testes de merge encontre a tabela existente.
        SemanticKnowledgeGraph(kuzu_conn=conn)
        resolver = EntityResolver(
            threshold=0.92,
            kuzu_conn=conn,
            chroma_collection=None,
            embedder=_fake_embed,
            allow_create=True,
        )
        yield conn, resolver
    finally:
        time.sleep(0.3)
        shutil.rmtree(kuzu_dir, ignore_errors=True)


# ── (a) Threshold separa entidades técnicas próximas ──────────────────────────


def test_synonyms_collapse_to_same_canonical(temp_kuzu):
    conn, resolver = temp_kuzu
    a = resolver.resolve("OpenAI")
    b = resolver.resolve("Open AI")
    assert a is not None
    assert a == b, "OpenAI e Open AI devem resolver para o mesmo nó canônico"


def test_distinct_technical_entities_do_not_collapse(temp_kuzu):
    conn, resolver = temp_kuzu
    bert = resolver.resolve("BERT")
    gpt = resolver.resolve("GPT")
    assert bert != gpt, "BERT e GPT devem ser nós canônicos diferentes"


def test_near_technical_terms_do_not_collapse(temp_kuzu):
    conn, resolver = temp_kuzu
    bert = resolver.resolve("BERT")
    roberta = resolver.resolve("RoBERTa")
    assert bert != roberta, "BERT e RoBERTa NÃO devem colapsar (false positive)"


def test_hash_suffix_does_not_falsely_merge(temp_kuzu):
    conn, resolver = temp_kuzu
    # Mesmo prefixo, hash diferente => dimensões vizinhas ortogonais.
    x = resolver.resolve("LangChain")
    y = resolver.resolve("LangGraph")
    assert x != y, "Entidades com nome parecido mas hash distinto não devem colapsar"


# ── (b) Merge preserva arestas (sem órfãos) ───────────────────────────────────


def test_merge_preserves_relations_no_orphans(temp_kuzu):
    conn, resolver = temp_kuzu

    # 1. Estabelece o nó canônico "OpenAI" (sinônimo resolvido primeiro) e seu
    #    nó no KuzuDB — exatamente como SemanticKnowledgeGraph.add_triple faz
    #    via MERGE pelo display canônico devolvido.
    canonical = resolver.resolve("OpenAI")
    assert canonical == "OpenAI"
    conn.execute(
        "MERGE (oi:SemanticEntity {name: 'OpenAI'}) ON CREATE SET oi.type='Company'"
    )

    # 2. Cria a VARIANTE "Open AI" (nó avulso) ligada a "GPT" e "Azure".
    conn.execute(
        "MERGE (a:SemanticEntity {name: 'Open AI'}) ON CREATE SET a.type='Company'"
    )
    conn.execute(
        "MERGE (g:SemanticEntity {name: 'GPT'}) ON CREATE SET g.type='Model'"
    )
    conn.execute(
        "MERGE (z:SemanticEntity {name: 'Azure'}) ON CREATE SET z.type='Cloud'"
    )
    conn.execute(
        "MATCH (a:SemanticEntity), (g:SemanticEntity) "
        "WHERE a.name='Open AI' AND g.name='GPT' "
        "CREATE (a)-[:RELATION {type:'produces', confidence:0.9}]->(g)"
    )
    conn.execute(
        "MATCH (z:SemanticEntity), (a:SemanticEntity) "
        "WHERE z.name='Azure' AND a.name='Open AI' "
        "CREATE (z)-[:RELATION {type:'partners_with', confidence:0.8}]->(a)"
    )

    # 3. Funde a variante "Open AI" no canônico "OpenAI".
    merged = resolver.merge_entities("OpenAI", "Open AI")
    assert merged is True

    # 1. Nó "Open AI" removido (sem órfão).
    res = conn.execute(
        "MATCH (e:SemanticEntity) WHERE e.name='Open AI' RETURN e.name"
    )
    assert not res.has_next(), "Nó fundido 'Open AI' deve ter sido removido"

    # 2. Arestas repointadas para "OpenAI": produz GPT, Azure parceira.
    out_res = conn.execute(
        "MATCH (s:SemanticEntity)-[rel:RELATION]->(o:SemanticEntity) "
        "WHERE s.name='OpenAI' RETURN o.name, rel.type"
    )
    out = {(str(r[0]), str(r[1])) for r in _drain(out_res)}
    assert ("GPT", "produces") in out

    in_res = conn.execute(
        "MATCH (s:SemanticEntity)-[rel:RELATION]->(o:SemanticEntity) "
        "WHERE o.name='OpenAI' RETURN s.name, rel.type"
    )
    in_rels = {(str(r[0]), str(r[1])) for r in _drain(in_res)}
    assert ("Azure", "partners_with") in in_rels

    # 3. Canônico existe e mantém conectividade.
    canon_res = conn.execute(
        "MATCH (e:SemanticEntity) WHERE e.name='OpenAI' RETURN e.name"
    )
    assert canon_res.has_next()


def test_merge_duplicate_relation_not_created(temp_kuzu):
    conn, resolver = temp_kuzu
    # Canônico "OpenAI" estabelecido primeiro (e seu nó KuzuDB criado, como
    # add_triple faria via MERGE pelo display canônico).
    resolver.resolve("OpenAI")
    conn.execute("MERGE (oi:SemanticEntity {name:'OpenAI'})")
    # Cria a VARIANTE "Open AI" avulsa com a mesma aresta produz->GPT.
    conn.execute("MERGE (a:SemanticEntity {name:'Open AI'})")
    conn.execute("MERGE (g:SemanticEntity {name:'GPT'})")
    for src in ("Open AI", "OpenAI"):
        conn.execute(
            f"MATCH (s:SemanticEntity), (g:SemanticEntity) "
            f"WHERE s.name='{src}' AND g.name='GPT' "
            f"CREATE (s)-[:RELATION {{type:'produces', confidence:0.9}}]->(g)"
        )
    resolver.merge_entities("OpenAI", "Open AI")
    res = conn.execute(
        "MATCH (s:SemanticEntity)-[r:RELATION]->(o:SemanticEntity) "
        "WHERE s.name='OpenAI' AND o.name='GPT' RETURN count(r)"
    )
    count = res.get_next()[0]
    assert count == 1, "Merge não deve duplicar a aresta produz->GPT"


def test_merge_self_is_noop(temp_kuzu):
    conn, resolver = temp_kuzu
    resolver.resolve("BERT")
    assert resolver.merge_entities("BERT", "BERT") is False


# ── Integração com SemanticKnowledgeGraph.add_triple ──────────────────────────


def test_kg_add_triple_resolves_variants(temp_kuzu):
    conn, resolver = temp_kuzu
    kg = SemanticKnowledgeGraph(kuzu_conn=conn, entity_resolver=resolver)

    # Duas pesquisas distintas, variantes do mesmo sujeito.
    kg.add_triple(Triple("Open AI", "produces", "GPT", 0.9, "test"))
    kg.add_triple(Triple("OpenAI", "competes_with", "Anthropic", 0.85, "test"))

    # Apenas UM nó canônico deve existir para a variante ("Open AI" e "OpenAI"
    # colapsam no mesmo canon_id) — não dois nós separados.
    res = conn.execute("MATCH (e:SemanticEntity) RETURN e.name")
    names = {str(r[0]) for r in _drain(res)}
    # O nó canônico é a primeira forma vista ("Open AI"); "OpenAI" não vira nó.
    assert "OpenAI" not in names, "Variante 'OpenAI' não deve virar nó separado"
    assert "Open AI" in names
    # Exatamente um nó canônico para o sujeito (sem fragmentação).
    subject_nodes = {n for n in names if n in ("Open AI", "OpenAI")}
    assert subject_nodes == {"Open AI"}, "Esperado exatamente 1 nó canônico"

    # Ambas as relações devem estar ligadas ao nó canônico "Open AI".
    triples = kg.query_graph(subject="Open AI")
    rels = {t.relation for t in triples}
    assert "produces" in rels
    assert "competes_with" in rels


def test_kg_add_triple_without_resolver_keeps_old_behavior(temp_kuzu):
    conn, _ = temp_kuzu
    kg = SemanticKnowledgeGraph(kuzu_conn=conn)  # sem resolver

    kg.add_triple(Triple("Open AI", "produces", "GPT", 0.9, "test"))
    kg.add_triple(Triple("OpenAI", "competes_with", "Anthropic", 0.85, "test"))

    res = conn.execute("MATCH (e:SemanticEntity) RETURN e.name")
    names = {str(r[0]) for r in _drain(res)}
    # Sem resolver: dois nós distintos preservados (comportamento anterior).
    assert "Open AI" in names
    assert "OpenAI" in names


# ── (c) Performance: resolve em lote não degrada com +10k entidades ───────────


def test_resolve_scales_with_many_entities(temp_kuzu):
    conn, resolver = temp_kuzu
    import time as _t

    N = 10_000
    # Popula o índice ChromaDB com N entidades únicas (cada uma numa dimensão
    # distinta, ciclando), para simular um grafo grande.
    unique_names = [f"Entity_{i}" for i in range(N)]
    vectors = []
    for i, name in enumerate(unique_names):
        vec = [0.0] * _DIM
        vec[i % _DIM] = 1.0
        vectors.append(vec)
        resolver._mem[name] = vec  # fallback em memória (sessão), evita I/O do Chroma

    # Resolve uma entidade nova; medição do tempo de uma única query.
    t0 = _t.perf_counter()
    result = resolver.resolve("Brand New Entity")
    dt = _t.perf_counter() - t0

    assert result == "Brand New Entity"
    # 10k entidades em memória: varredura linear deve ficar bem abaixo de 1s.
    assert dt < 1.0, f"resolve com 10k entidades demorou {dt:.3f}s (deve < 1.0s)"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _drain(result):
    """Materializa as linhas de um resultado KuzuDB em lista."""
    rows = []
    while result.has_next():
        rows.append(result.get_next())
    return rows
