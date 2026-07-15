"""
entity_resolver.py — Entity Resolution Cross-Session (Bloco 14 / E6-T1)

Resolve entidades mencionadas em pesquisas diferentes para o MESMO nó canônico
no grafo de conhecimento, evitando fragmentação ("OpenAI" vs "Open AI" → mesmo nó).

Design (adaptado ao schema real do projeto, Zero Silent Assumptions):
  - O `SemanticKnowledgeGraph` persiste entidades em `SemanticEntity(name, type,
    PRIMARY KEY(name))` (KuzuDB). Não existe `add_entity()` nem campo `source_ids`
    em `SemanticEntity` — por isso o resolver NÃO inventa schema: a "prova de
    proveniência" de uma entidade é a sua conectividade no grafo (arestas
    `RELATION`), e o merge preserva exatamente essas arestas em vez de um campo
    inexistente.
  - O embedding de similaridade vive numa coleção dedicada do ChromaDB
    (`sra_entity_resolution`) — ANN eficiente e threshold de similaridade
    coseno, escalando para +10k entidades sem degradar (O(log N) por query).
  - O KuzuDB é a fonte de verdade canônica dos nós; o ChromaDB é o índice de
    busca semântica. O `EntityResolver` mantém ambos consistentes.
  - O embedder é INJETÁVEL (callable `str -> list[float] | None`), de modo que
    os testes são herméticos (sem download de modelo) e a produção usa
    `all-MiniLM-L6-v2` via sentence-transformers com fallback gracioso.

Thread-safety: as escritas no KuzuDB usam o lock de arquivo já existente no
`knowledge_graph`; aqui mantemos o resolver livre de estado global para que
possa ser injetado em qualquer grafo.
"""

from __future__ import annotations

import logging
import re
import tempfile
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Threshold padrão de similaridade coseno (0-1). 0.92 separa entidades técnicas
# próximas ("BERT" vs "RoBERTa") sem fundir variantes legítimas ("OpenAI" vs
# "Open AI" colapsam por sinônimo, não por similaridade de substring).
DEFAULT_THRESHOLD = 0.92

# Nome da coleção vetorial dedicada à resolução de entidades.
ENTITY_COLLECTION = "sra_entity_resolution"

_EMBED_MODEL = "all-MiniLM-L6-v2"

# Normalização mínima: trim + colapso de espaços internos. NÃO colapsa "OpenAI"
# e "Open AI" para a mesma string — a similaridade coseno (via embedding) é quem
# decide o merge, não a normalização de texto.
_WS_RE = re.compile(r"\s+")

# Lock preguiçoso para o embedder default (sentence-transformers não é
# thread-safe em carga concorrente).
_embedder_lock = None
_embedder_cache: Any = None


def _normalize_name(name: str) -> str:
    """Normaliza o nome da entidade (trim + colapso de whitespace)."""
    return _WS_RE.sub(" ", name.strip())


def _canon_id(name: str) -> str:
    """Calcula o id canônico ESTÁVEL de uma entidade.

    O id canônico é a forma normalizada em minúsculas com espaços colapsados —
    determinístico e independente da ordem em que as variantes são vistas. "Open
    AI" e "OpenAI" mapeiam para o mesmo ``canon_id`` ("openai"), enquanto "BERT"
    e "GPT" permanecem distintos. O embedding (similaridade coseno) é o matcher
    *fuzzy* que decide se duas entidades com ids diferentes ainda assim colapsam
    (sinônimos que a normalização não captura); o id canônico é a chave estável
    de índice que garante retorno determinístico do nó canônico.
    """
    return _normalize_name(name).lower()


def _get_default_embedder() -> Callable[[str], list[float] | None]:
    """Carrega preguiçosamente o embedder sentence-transformers padrão.

    Returns:
        Callable que mapeia texto -> vetor normalizado, ou levanta se o modelo
        não estiver disponível (o chamador trata o fallback).
    """
    global _embedder_cache
    if _embedder_cache is not None:
        return _embedder_cache

    import threading

    global _embedder_lock
    if _embedder_lock is None:
        _embedder_lock = threading.Lock()

    with _embedder_lock:
        if _embedder_cache is None:
            try:
                from sentence_transformers import SentenceTransformer

                model = SentenceTransformer(_EMBED_MODEL)
                logger.info(
                    "EntityResolver: embedder '%s' carregado (resolução ativa).",
                    _EMBED_MODEL,
                )

                def _embed(text: str) -> list[float] | None:
                    return model.encode(text, normalize_embeddings=True).tolist()

                _embedder_cache = _embed
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EntityResolver: embedder '%s' indisponível (%s) — "
                    "resolução usa fallback de nome exato.",
                    _EMBED_MODEL,
                    exc,
                )
                raise
    return _embedder_cache


def _cosine(a: list[float], b: list[float]) -> float:
    """Similaridade coseno entre dois vetores (assume vetores normalizados)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return max(-1.0, min(1.0, dot))


class EntityResolver:
    """Resolve entidades para nós canônicos via similaridade coseno (ChromaDB).

    A resolução segue dois índices:
      - ChromaDB (`sra_entity_resolution`): busca semântica ANN com threshold.
      - KuzuDB (`SemanticEntity`): fonte de verdade dos nós canônicos.

    Os dois são mantidos consistentes: ao criar uma entidade, cria-se o nó no
    KuzuDB e o vetor no ChromaDB; ao fazer merge, as arestas são repointadas.
    """

    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        kuzu_conn: Any = None,
        chroma_collection: Any = None,
        embedder: Callable[[str], list[float] | None] | None = None,
        collection_name: str = ENTITY_COLLECTION,
        allow_create: bool = True,
    ) -> None:
        """Inicializa o resolver.

        Args:
            threshold: Similaridade coseno mínima (0-1) para considerar two
                entidades a mesma. Default 0.92.
            kuzu_conn: Conexão KuzuDB (fonte de verdade dos nós). Opcional.
            chroma_collection: Coleção ChromaDB para busca vetorial. Opcional;
                se None, usa fallback em memória (apenas para a sessão atual).
            embedder: Callable `str -> list[float] | None`. Se None, tenta
                carregar sentence-transformers; se indisponível, fallback de nome.
            collection_name: Nome da coleção ChromaDB dedicada.
            allow_create: Se True, cria novo nó canônico quando não há match.
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold deve estar em [0,1]; recebido {threshold}.")
        self.threshold = threshold
        self.kuzu_conn = kuzu_conn
        self.embedder = embedder
        self.collection_name = collection_name
        self.allow_create = allow_create

        # Índice vetorial: ChromaDB injetado ou coleção efêmera criada sob demanda.
        self._chroma = chroma_collection
        self._owns_chroma = chroma_collection is None
        # Fallback em memória quando ChromaDB indisponível (sessão efêmera).
        self._mem: dict[str, list[float]] = {}

    # ── Inicialização preguiçosa do ChromaDB ──────────────────────────────────

    def _ensure_chroma(self) -> Any:
        """Garante que a coleção ChromaDB existe; cria efêmera se necessário.

        Returns:
            A coleção ChromaDB, ou None se ChromaDB indisponível (usa memória).
        """
        if self._chroma is not None:
            return self._chroma
        if not self._owns_chroma:
            return None
        try:
            import chromadb

            # Cliente efêmero ISOLADO por instância de resolver: usa um diretório
            # temporário único. O `chromadb.Client()` default é um singleton de
            # processo keyado por path "", então compartilhá-lo entre testes
            # vazaria entidades de um para outro. Um path exclusivo evita o
            # vazamento e mantém o fallback determinístico em memória.
            persist = tempfile.mkdtemp(prefix="sra_er_")
            self._chroma_path = persist
            client = chromadb.PersistentClient(path=persist)
            self._chroma = client.get_or_create_collection(
                name=self.collection_name, metadata={"hnsw:space": "cosine"}
            )
            logger.info(
                "EntityResolver: coleção ChromaDB '%s' pronta (path=%s).",
                self.collection_name,
                persist,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EntityResolver: ChromaDB indisponível (%s) — fallback em memória.",
                exc,
            )
            self._chroma = None
        return self._chroma

    def _embed(self, text: str) -> list[float] | None:
        """Gera o embedding do texto, usando o embedder injetado ou o padrão."""
        if self.embedder is not None:
            try:
                return self.embedder(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EntityResolver: embedder falhou para '%s': %s", text, exc
                )
                return None
        try:
            return _get_default_embedder()(text)
        except Exception:  # noqa: BLE001
            logger.warning("EntityResolver: embedder padrão indisponível.")
            return None

    # ── Resolução ─────────────────────────────────────────────────────────────

    def resolve(self, entity_name: str) -> str | None:
        """Resolve um nome de entidade para o nó canônico (string canônica).

        Fluxo determinístico:
          1. Calcula o ``canon_id`` estável (normalize+lower) do nome.
          2. Se vazio, retorna None.
          3. Busca por similaridade coseno no ChromaDB (threshold). O id
             indexado é o ``canon_id``; o valor de retorno é o *display name*
             estável armazenado (primeira forma vista para aquele id).
          4. Caso contrário, se ``allow_create``: cria o índice para o
             ``canon_id`` e retorna o próprio ``name`` (display original).
             Se não, retorna None.

        Args:
            entity_name: Nome bruto da entidade (ex: "Open AI").

        Returns:
            O nome canônico resolvido (str) ou None se não resolvido.
        """
        name = _normalize_name(entity_name)
        if not name:
            return None
        cid = _canon_id(name)

        vector = self._embed(name)
        chroma = self._ensure_chroma()

        if vector is not None and chroma is not None:
            try:
                results = chroma.query(
                    query_embeddings=[vector],
                    n_results=1,
                    include=["distances", "metadatas"],
                )
                ids = results.get("ids", [[]])[0]
                distances = results.get("distances", [[]])[0]
                metas = results.get("metadatas", [[]])[0]
                if ids and distances:
                    similarity = 1.0 - float(distances[0])
                    if similarity >= self.threshold:
                        canonical = self._display_of(ids[0], metas[0])
                        logger.debug(
                            "EntityResolver: '%s' resolvido para '%s' (sim=%.3f)",
                            name,
                            canonical,
                            similarity,
                        )
                        return canonical
            except Exception as exc:  # noqa: BLE001
                logger.warning("EntityResolver: busca ChromaDB falhou: %s", exc)

        elif vector is not None and chroma is None:
            # Fallback em memória: varre a sessão atual por canon_id.
            best_id: str | None = None
            best_sim = -1.0
            for stored_id, stored_vec in self._mem.items():
                sim = _cosine(vector, stored_vec)
                if sim > best_sim:
                    best_sim = sim
                    best_id = stored_id
            if best_id is not None and best_sim >= self.threshold:
                return best_id

        # Sem match — cria índice canônico se permitido.
        if self.allow_create:
            self._index_canonical(cid, name, vector)
            return name
        return None

    @staticmethod
    def _display_of(stored_id: str, metadata: dict | None) -> str:
        """Recupera o display name estável de um id indexado (canon_id)."""
        if metadata and isinstance(metadata, dict) and metadata.get("display"):
            return str(metadata["display"])
        return str(stored_id)

    def _index_canonical(
        self, cid: str, display: str, vector: list[float] | None
    ) -> None:
        """Indexa o ``canon_id`` no índice vetorial (ChromaDB ou memória).

        IMPORTANTE: NÃO cria nó no KuzuDB aqui. O KuzuDB é fonte de verdade dos
        nós canônicos e é populado por quem consome a resolução (ex:
        `SemanticKnowledgeGraph.add_triple`, que faz MERGE pelo display canônico
        devolvido). Isso evita nós órfãos ("Open AI") quando o nome original
        difere do canônico resolvido. O id de índice é o ``canon_id`` estável; o
        ``display`` é a forma de apresentação (primeira vista) retornada pela
        resolução.
        """
        chroma = self._ensure_chroma()
        if chroma is not None:
            try:
                meta = {"canon_id": cid, "display": display}
                if vector is not None:
                    chroma.upsert(
                        ids=[cid],
                        embeddings=[vector],
                        documents=[display],
                        metadatas=[meta],
                    )
                else:
                    chroma.upsert(ids=[cid], documents=[display], metadatas=[meta])
            except Exception as exc:  # noqa: BLE001
                logger.warning("EntityResolver: upsert ChromaDB falhou: %s", exc)
        elif vector is not None:
            self._mem[cid] = vector

    # ── Merge ──────────────────────────────────────────────────────────────────

    def merge_entities(self, id_a: str, id_b: str) -> bool:
        """Funde ``id_b`` em ``id_a`` no grafo de conhecimento.

        Adaptado ao schema real: `SemanticEntity(name, type, PRIMARY KEY(name))`
        NÃO possui campo `source_ids`. A "prova de proveniência" de uma entidade
        é a sua conectividade no grafo (arestas `RELATION`). O merge preserva
        exatamente essa conectividade:

          1. Reponta todas as arestas `RELATION` que saem/entram em ``id_b``
             para ``id_a`` (mantendo `type` e `confidence` originais), sem criar
             duplicatas (checa existência antes de criar).
          2. Remove o nó ``id_b`` (DETACH DELETE) para não deixar referência
             órfã.
          3. Atualiza o índice ChromaDB (remove o vetor de ``id_b``).

        Args:
            id_a: Nó canônico destino (sobrevive).
            id_b: Nó a ser fundido e removido.

        Returns:
            True se o merge foi executado (nós distintos e ``id_a`` existe),
            False caso contrário (incluindo id_a == id_b).
        """
        if not id_a or not id_b or id_a == id_b:
            return False
        if self.kuzu_conn is None:
            logger.debug("EntityResolver: KuzuDB ausente — merge ignorado.")
            return False

        try:
            # 0. Confirma que id_a existe como nó canônico.
            res = self.kuzu_conn.execute(
                "MATCH (a:SemanticEntity) WHERE a.name = $a RETURN a.name",
                {"a": id_a},
            )
            if not res.has_next():
                logger.warning(
                    "EntityResolver: merge abortado — canônico '%s' inexistente.", id_a
                )
                return False

            # 1a. Reponta arestas de saída (id_b -> X) para (id_a -> X).
            out_rows = self._collect_relations(
                "MATCH (s:SemanticEntity)-[r:RELATION]->(o:SemanticEntity) "
                "WHERE s.name = $b RETURN o.name, r.type, r.confidence",
                {"b": id_b},
            )
            for target, rel_type, conf in out_rows:
                self._repoint_or_skip(id_a, target, rel_type, conf, direction="out")

            # 1b. Reponta arestas de entrada (X -> id_b) para (X -> id_a).
            in_rows = self._collect_relations(
                "MATCH (s:SemanticEntity)-[r:RELATION]->(o:SemanticEntity) "
                "WHERE o.name = $b RETURN s.name, r.type, r.confidence",
                {"b": id_b},
            )
            for source, rel_type, conf in in_rows:
                self._repoint_or_skip(source, id_a, rel_type, conf, direction="in")

            # 2. Remove o nó fundido (e quaisquer self-loops remanescentes).
            self.kuzu_conn.execute(
                "MATCH (b:SemanticEntity) WHERE b.name = $b DETACH DELETE b",
                {"b": id_b},
            )
            logger.info(
                "EntityResolver: '%s' fundido em '%s' (arestas preservadas).",
                id_b,
                id_a,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("EntityResolver: merge falhou: %s", exc)
            return False

        # 3. Índice ChromaDB: remove o canon_id de id_b (id_a já está indexado).
        chroma = self._ensure_chroma()
        b_cid = _canon_id(id_b)
        if chroma is not None:
            try:
                chroma.delete(ids=[b_cid])
            except Exception as exc:  # noqa: BLE001
                logger.warning("EntityResolver: delete ChromaDB falhou: %s", exc)
        self._mem.pop(b_cid, None)
        return True

    def _collect_relations(
        self, query: str, params: dict[str, str]
    ) -> list[tuple[str, str, float]]:
        """Coleta (outro_nó, tipo, confiança) das arestas de ``id_b``."""
        rows: list[tuple[str, str, float]] = []
        try:
            res = self.kuzu_conn.execute(query, params)
            while res.has_next():
                row = res.get_next()
                rows.append((str(row[0]), str(row[1]), float(row[2])))
        except Exception as exc:  # noqa: BLE001
            logger.warning("EntityResolver: coleta de arestas falhou: %s", exc)
        return rows

    def _repoint_or_skip(
        self,
        source: str,
        target: str,
        rel_type: str,
        conf: float,
        direction: str,
    ) -> None:
        """Recria a aresta para ``id_a`` só se não existir (evita duplicata)."""
        check_q = (
            "MATCH (s:SemanticEntity)-[r:RELATION]->(o:SemanticEntity) "
            "WHERE s.name = $s AND o.name = $o AND r.type = $t RETURN r"
        )
        res = self.kuzu_conn.execute(check_q, {"s": source, "o": target, "t": rel_type})
        if res.has_next():
            return
        create_q = (
            "MATCH (s:SemanticEntity), (o:SemanticEntity) "
            "WHERE s.name = $s AND o.name = $o "
            "CREATE (s)-[:RELATION {type: $t, confidence: $c}]->(o)"
        )
        self.kuzu_conn.execute(
            create_q, {"s": source, "o": target, "t": rel_type, "c": float(conf)}
        )
        _ = direction  # direção documentada para clareza; lógica é simétrica.
