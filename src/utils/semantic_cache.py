"""Semantic Cache para Smart Research Agent.

Implementa cache por similaridade semântica usando embeddings de queries,
permitindo reutilização de resultados para queries semanticamente similares.

Características:
- Embeddings via sentence-transformers (all-MiniLM-L6-v2)
- Similaridade cosseno with threshold configurável (padrão 90%)
- TTL adaptativo por source (integrado com Cache existente)
- Cache warming para queries populares
- Integração com ChromaDB para busca vetorial
- Fallback em memória quando ChromaDB indisponível

Uso:
    cache = SemanticCache()

    # Buscar resultado semanticamente similar
    result = await cache.get("melhores frameworks python 2024")

    # Armazenar resultado
    await cache.set("melhores frameworks python 2024", data, source="github")

    # Cache warming
    await cache.warm([
        "frameworks python",
        "bibliotecas machine learning",
        "ferramentas devops"
    ])
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import numpy as np

logger = logging.getLogger("utils.semantic_cache")

# ── Configurações Padrão ─────────────────────────────────────────────────────
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DEFAULT_SIMILARITY_THRESHOLD = 0.90
DEFAULT_COLLECTION_NAME = "semantic_cache"

# TTL adaptativo por source (em segundos)
TTL_BY_SOURCE: dict[str, int] = {
    "github": 3600,  # 1 hora - repos mudam frequentemente
    "reddit": 1800,  # 30 minutos - discussões são voláteis
    "hackernews": 1800,  # 30 minutos
    "news": 900,  # 15 minutos - notícias são muito voláteis
    "rss": 1800,  # 30 minutos
    "stackoverflow": 3600,  # 1 hora
    "papers": 86400,  # 24 horas - papers são estáveis
    "arxiv": 86400,  # 24 horas
    "documentation": 604800,  # 7 dias - docs mudam pouco
    "default": 3600,  # 1 hora
}

# Queries populares para cache warming
POPULAR_QUERIES: list[str] = [
    "melhores frameworks python",
    "bibliotecas machine learning python",
    "ferramentas devops",
    "frameworks javascript",
    "plataformas saas b2b",
    "ferramentas ia generativa",
    "frameworks web rust",
    "bibliotecas data science",
    "ferramentas observabilidade",
    "plataformas cloud",
]


class _InMemoryEmbeddingStore:
    """Fallback de armazenamento de embeddings em memória."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def add(
        self,
        query: str,
        embedding: list[float],
        data: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        """Adiciona embedding ao store."""
        query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
        self._store[query_hash] = {
            "query": query,
            "embedding": embedding,
            "data": data,
            "metadata": metadata,
            "created_at": time.time(),
        }

    def search(
        self,
        query_embedding: list[float],
        threshold: float,
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        """Busca embeddings similares por cosseno."""
        results = []
        query_vec = np.array(query_embedding)

        for doc_id, doc in self._store.items():
            doc_vec = np.array(doc["embedding"])

            # Similaridade cosseno
            denom = np.linalg.norm(query_vec) * np.linalg.norm(doc_vec)
            if denom == 0:
                similarity = 0.0
            else:
                similarity = float(np.dot(query_vec, doc_vec) / denom)

            if similarity >= threshold:
                results.append(
                    {
                        "id": doc_id,
                        "similarity": similarity,
                        "document": doc["data"],
                        "metadata": doc["metadata"],
                        "query": doc["query"],
                    }
                )

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]

    def delete(self, query_hash: str) -> None:
        """Remove embedding do store."""
        self._store.pop(query_hash, None)

    def clear(self) -> None:
        """Limpa todo o store."""
        self._store.clear()

    def count(self) -> int:
        """Retorna número de embeddings armazenados."""
        return len(self._store)

    @property
    def backend_name(self) -> str:
        return "in_memory"


class SemanticCache:
    """Cache semântico com busca por similaridade de embeddings.

    Usa sentence-transformers para gerar embeddings de queries e
    ChromaDB (ou fallback em memória) para busca vetorial eficiente.

    Atributos:
        embedding_model: Modelo de embeddings utilizado.
        similarity_threshold: Threshold mínimo de similaridade (0.0-1.0).
        collection_name: Nome da coleção no ChromaDB.
        backend: Backend de armazenamento ("chromadb" ou "in_memory").
    """

    def __init__(
        self,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        collection_name: str = DEFAULT_COLLECTION_NAME,
        chromadb_host: str | None = None,
        chromadb_port: int | None = None,
        persist_directory: str | None = None,
        threshold: float | None = None,
    ) -> None:
        """Inicializa o SemanticCache.

        Args:
            embedding_model: Nome do modelo sentence-transformers.
            similarity_threshold: Threshold de similaridade cosseno (0.0-1.0).
            collection_name: Nome da coleção no ChromaDB.
            chromadb_host: Host do ChromaDB (se usando cliente remoto).
            chromadb_port: Porta do ChromaDB.
            persist_directory: Diretório para persistência local do ChromaDB.
            threshold: Parâmetro de compatibilidade para similarity_threshold.
        """
        self.embedding_model = embedding_model
        self.similarity_threshold = (
            threshold if threshold is not None else similarity_threshold
        )
        self.collection_name = collection_name
        self._embedding_model_instance = None
        self._backend: Any = None
        self._collection: Any = None
        self._is_chromadb = False

        # Inicializa modelo de embeddings (lazy loading)
        self._init_embedding_model()

        # Inicializa backend de armazenamento
        self._init_backend(chromadb_host, chromadb_port, persist_directory)

    def _init_embedding_model(self) -> None:
        """Inicializa o modelo de embeddings (lazy loading)."""
        try:
            from sentence_transformers import SentenceTransformer

            self._embedding_model_instance = SentenceTransformer(self.embedding_model)
            logger.info(f"SemanticCache: Modelo '{self.embedding_model}' carregado")
        except Exception as e:
            logger.warning(
                f"SemanticCache: Falha ao carregar modelo de embeddings: {e}. "
                "Busca semântica desabilitada."
            )
            self._embedding_model_instance = None

    def _init_backend(
        self,
        chromadb_host: str | None,
        chromadb_port: int | None,
        persist_directory: str | None,
    ) -> None:
        """Inicializa o backend de armazenamento vetorial."""
        # Tentar ChromaDB
        try:
            import chromadb

            if chromadb_host and chromadb_port:
                # Cliente remoto
                client = chromadb.HttpClient(
                    host=chromadb_host,
                    port=chromadb_port,
                )
                logger.info(
                    f"SemanticCache: ChromaDB conectado em "
                    f"{chromadb_host}:{chromadb_port}"
                )
            elif persist_directory:
                # Persistência local
                client = chromadb.PersistentClient(path=persist_directory)
                logger.info(
                    f"SemanticCache: ChromaDB persistente em {persist_directory}"
                )
            else:
                # Cliente efêmero (em memória)
                client = chromadb.EphemeralClient()
                logger.info("SemanticCache: ChromaDB efêmero (em memória)")

            self._backend = client
            self._collection = client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            self._is_chromadb = True
            logger.info(
                f"SemanticCache: Coleção '{self.collection_name}' pronta "
                f"({self._collection.count()} embeddings)"
            )

        except Exception as e:
            logger.warning(
                f"SemanticCache: ChromaDB indisponível ({e}). "
                "Usando fallback em memória."
            )
            self._backend = _InMemoryEmbeddingStore()
            self._is_chromadb = False

    @property
    def backend_name(self) -> str:
        """Retorna nome do backend ativo."""
        if self._is_chromadb:
            return "chromadb"
        return self._backend.backend_name

    @property
    def enabled(self) -> bool:
        """Indica se o cache semântico está funcional (modelo de embeddings carregado)."""
        return self._embedding_model_instance is not None

    def _get_embedding(self, text: str) -> list[float]:
        """Gera embedding para um texto.

        Args:
            text: Texto para gerar embedding.

        Returns:
            Lista de floats representando o embedding.

        Raises:
            RuntimeError: Se modelo de embeddings não estiver disponível.
        """
        if self._embedding_model_instance is None:
            raise RuntimeError(
                "Modelo de embeddings não disponível. "
                "Verifique se sentence-transformers está instalado."
            )

        embedding = self._embedding_model_instance.encode(
            text,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return embedding.tolist()

    def _compute_ttl(self, source: str) -> int:
        """Computa TTL adaptativo baseado no source.

        Args:
            source: Nome da fonte (github, reddit, papers, etc).

        Returns:
            TTL em segundos.
        """
        return TTL_BY_SOURCE.get(source, TTL_BY_SOURCE["default"])

    async def get(
        self,
        query: str,
        source: str | None = None,
    ) -> dict[str, Any] | None:
        """Busca resultado semanticamente similar no cache.

        Args:
            query: Query de busca.
            source: Filtrar por source específico (opcional).

        Returns:
            Dict com 'data', 'similarity', 'cached_query', 'metadata' ou None.
        """
        if self._embedding_model_instance is None:
            logger.debug("SemanticCache: Embeddings desabilitados, pulando busca")
            return None

        try:
            # Gerar embedding da query
            query_embedding = self._get_embedding(query)

            # Buscar no backend
            if self._is_chromadb:
                results = self._collection.query(
                    query_embeddings=[query_embedding],
                    n_results=5,
                    include=["metadatas", "documents", "distances"],
                )

                if not results["ids"] or not results["ids"][0]:
                    return None

                # Processar resultados
                for i, doc_id in enumerate(results["ids"][0]):
                    metadata = results["metadatas"][0][i]
                    distance = results["distances"][0][i]

                    # ChromaDB retorna distância, converter para similaridade
                    # Para cosine distance: similarity = 1 - distance
                    similarity = 1.0 - distance

                    if similarity < self.similarity_threshold:
                        continue

                    # Filtrar por source se especificado
                    if source and metadata.get("source") != source:
                        continue

                    # Verificar TTL
                    cached_at = metadata.get("cached_at", 0)
                    ttl = metadata.get("ttl", TTL_BY_SOURCE["default"])
                    if time.time() - cached_at > ttl:
                        # Expirado, remover
                        self._collection.delete(ids=[doc_id])
                        continue

                    # Cache hit!
                    data = json.loads(results["documents"][0][i])
                    cached_query = metadata.get("query", "")

                    logger.info(
                        f"SemanticCache HIT: '{query[:50]}' -> "
                        f"'{cached_query[:50]}' (similarity={similarity:.3f})"
                    )

                    return {
                        "data": data,
                        "similarity": similarity,
                        "cached_query": cached_query,
                        "metadata": metadata,
                    }

            else:
                # Fallback em memória
                results = self._backend.search(
                    query_embedding,
                    self.similarity_threshold,
                    limit=5,
                )

                for result in results:
                    metadata = result["metadata"]

                    # Filtrar por source
                    if source and metadata.get("source") != source:
                        continue

                    # Verificar TTL
                    cached_at = metadata.get("cached_at", 0)
                    ttl = metadata.get("ttl", TTL_BY_SOURCE["default"])
                    if time.time() - cached_at > ttl:
                        # Expirado, remover
                        query_hash = hashlib.sha256(
                            result["query"].encode()
                        ).hexdigest()[:16]
                        self._backend.delete(query_hash)
                        continue

                    # Cache hit!
                    logger.info(
                        f"SemanticCache HIT: '{query[:50]}' -> "
                        f"'{result['query'][:50]}' "
                        f"(similarity={result['similarity']:.3f})"
                    )

                    return {
                        "data": result["document"],
                        "similarity": result["similarity"],
                        "cached_query": result["query"],
                        "metadata": metadata,
                    }

            return None

        except Exception as e:
            logger.warning(f"SemanticCache.get falhou: {e}")
            return None

    async def set(
        self,
        query: str,
        data: dict[str, Any],
        source: str = "default",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Armazena resultado no cache semântico.

        Args:
            query: Query original.
            data: Dados a serem cacheados.
            source: Fonte dos dados (para TTL adaptativo).
            metadata: Metadados adicionais (opcional).
        """
        if self._embedding_model_instance is None:
            logger.debug("SemanticCache: Embeddings desabilitados, pulando set")
            return

        try:
            # Gerar embedding
            query_embedding = self._get_embedding(query)

            # Preparar metadados
            ttl = self._compute_ttl(source)
            full_metadata = {
                "query": query,
                "source": source,
                "cached_at": time.time(),
                "ttl": ttl,
                "expires_at": time.time() + ttl,
                **(metadata or {}),
            }

            # Gerar ID único
            query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]

            if self._is_chromadb:
                # Serializar dados como documento
                document = json.dumps(data, default=str)

                self._collection.upsert(
                    ids=[query_hash],
                    embeddings=[query_embedding],
                    documents=[document],
                    metadatas=[full_metadata],
                )
            else:
                # Fallback em memória
                self._backend.add(
                    query=query,
                    embedding=query_embedding,
                    data=data,
                    metadata=full_metadata,
                )

            logger.info(
                f"SemanticCache SET: '{query[:50]}' [source={source}, ttl={ttl}s]"
            )

        except Exception as e:
            logger.warning(f"SemanticCache.set falhou: {e}")

    async def delete(self, query: str) -> None:
        """Remove query específica do cache.

        Args:
            query: Query para remover.
        """
        try:
            query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]

            if self._is_chromadb:
                self._collection.delete(ids=[query_hash])
            else:
                self._backend.delete(query_hash)

            logger.info(f"SemanticCache DELETE: '{query[:50]}'")

        except Exception as e:
            logger.warning(f"SemanticCache.delete falhou: {e}")

    async def clear(self) -> None:
        """Limpa todo o cache semântico."""
        try:
            if self._is_chromadb:
                # Recriar coleção
                self._backend.delete_collection(self.collection_name)
                self._collection = self._backend.get_or_create_collection(
                    name=self.collection_name,
                    metadata={"hnsw:space": "cosine"},
                )
            else:
                self._backend.clear()

            logger.info("SemanticCache: Cache limpo")

        except Exception as e:
            logger.warning(f"SemanticCache.clear falhou: {e}")

    async def warm(self, queries: list[str] | None = None) -> int:
        """Aquece o cache com queries populares.

        Pré-carrega embeddings para queries comuns, melhorando performance
        em buscas subsequentes.

        Args:
            queries: Lista de queries para aquecer. Se None, usa POPULAR_QUERIES.

        Returns:
            Número de queries processadas.
        """
        if self._embedding_model_instance is None:
            logger.warning("SemanticCache: Embeddings desabilitados, warm ignorado")
            return 0

        queries_to_warm = queries or POPULAR_QUERIES
        count = 0

        try:
            logger.info(
                f"SemanticCache: Aquecendo cache com {len(queries_to_warm)} queries"
            )

            # Gerar embeddings em batch (mais eficiente)
            embeddings = self._embedding_model_instance.encode(
                queries_to_warm,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=True,
            )

            for i, query in enumerate(queries_to_warm):
                query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
                embedding = embeddings[i].tolist()

                metadata = {
                    "query": query,
                    "source": "warmup",
                    "cached_at": time.time(),
                    "ttl": TTL_BY_SOURCE["default"],
                    "warmed": True,
                }

                if self._is_chromadb:
                    self._collection.upsert(
                        ids=[query_hash],
                        embeddings=[embedding],
                        documents=[""],  # Documento vazio, só embedding
                        metadatas=[metadata],
                    )
                else:
                    self._backend.add(
                        query=query,
                        embedding=embedding,
                        data={},
                        metadata=metadata,
                    )

                count += 1

            logger.info(f"SemanticCache: {count} queries aquecidas com sucesso")

        except Exception as e:
            logger.warning(f"SemanticCache.warm falhou: {e}")

        return count

    async def cleanup_expired(self) -> int:
        """Remove entradas expiradas do cache.

        Returns:
            Número de entradas removidas.
        """
        removed = 0

        try:
            if self._is_chromadb:
                # Buscar todos os metadados
                all_data = self._collection.get(include=["metadatas"])

                if not all_data["ids"]:
                    return 0

                ids_to_delete = []
                current_time = time.time()

                for i, doc_id in enumerate(all_data["ids"]):
                    metadata = all_data["metadatas"][i]
                    expires_at = metadata.get("expires_at", 0)

                    if current_time > expires_at:
                        ids_to_delete.append(doc_id)
                        removed += 1

                if ids_to_delete:
                    self._collection.delete(ids=ids_to_delete)

            else:
                # Fallback em memória
                current_time = time.time()
                expired_keys = []

                for doc_id, doc in self._backend._store.items():
                    metadata = doc["metadata"]
                    expires_at = metadata.get("expires_at", 0)

                    if current_time > expires_at:
                        expired_keys.append(doc_id)
                        removed += 1

                for key in expired_keys:
                    self._backend.delete(key)

            if removed > 0:
                logger.info(f"SemanticCache: {removed} entradas expiradas removidas")

        except Exception as e:
            logger.warning(f"SemanticCache.cleanup_expired falhou: {e}")

        return removed

    def stats(self) -> dict[str, Any]:
        """Retorna estatísticas do cache.

        Returns:
            Dict com estatísticas (total embeddings, backend, modelo, etc).
        """
        try:
            if self._is_chromadb:
                total = self._collection.count()
            else:
                total = self._backend.count()

            return {
                "backend": self.backend_name,
                "collection": self.collection_name,
                "total_embeddings": total,
                "embedding_model": self.embedding_model,
                "similarity_threshold": self.similarity_threshold,
                "model_loaded": self._embedding_model_instance is not None,
            }
        except Exception as e:
            logger.warning(f"SemanticCache.stats falhou: {e}")
            return {
                "backend": self.backend_name,
                "error": str(e),
            }

    def find(self, query: str, prefix: str | None = None) -> tuple[str, float] | None:
        """Busca semanticamente similar e retorna (matched_key, similarity)."""
        if self._embedding_model_instance is None:
            return None

        try:
            query_embedding = self._get_embedding(query)

            if self._is_chromadb:
                results = self._collection.query(
                    query_embeddings=[query_embedding],
                    n_results=5,
                    include=["metadatas", "distances"],
                )

                if not results["ids"] or not results["ids"][0]:
                    return None

                for i, doc_id in enumerate(results["ids"][0]):
                    metadata = results["metadatas"][0][i]
                    distance = results["distances"][0][i]
                    similarity = 1.0 - distance

                    if similarity < self.similarity_threshold:
                        continue

                    if prefix and metadata.get("prefix") != prefix:
                        continue

                    # Verificar TTL
                    cached_at = metadata.get("cached_at", 0)
                    ttl = metadata.get("ttl", TTL_BY_SOURCE["default"])
                    if time.time() - cached_at > ttl:
                        self._collection.delete(ids=[doc_id])
                        continue

                    matched_key = metadata.get("key", doc_id)
                    return matched_key, similarity
            else:
                results = self._backend.search(
                    query_embedding,
                    self.similarity_threshold,
                    limit=5,
                )
                for result in results:
                    metadata = result["metadata"]
                    if prefix and metadata.get("prefix") != prefix:
                        continue
                    cached_at = metadata.get("cached_at", 0)
                    ttl = metadata.get("ttl", TTL_BY_SOURCE["default"])
                    if time.time() - cached_at > ttl:
                        query_hash = hashlib.sha256(
                            result["query"].encode()
                        ).hexdigest()[:16]
                        self._backend.delete(query_hash)
                        continue

                    matched_key = metadata.get("key", result["query"])
                    return matched_key, result["similarity"]
            return None
        except Exception as e:
            logger.warning(f"SemanticCache.find falhou: {e}")
            return None

    def index(
        self,
        query: str,
        value: Any,
        prefix: str | None = None,
        ttl: int | None = None,
    ) -> None:
        """Indexa um valor no cache semântico de forma síncrona."""
        if self._embedding_model_instance is None:
            return

        try:
            # Extrai o texto real da query a ser vetorizado
            # O query recebido aqui é o 'key' do cache.py (ex: "search:melhores frameworks python")
            if ":" in query:
                query_text = query.split(":", 1)[1]
            else:
                query_text = query

            query_embedding = self._get_embedding(query_text)
            effective_ttl = ttl or TTL_BY_SOURCE["default"]

            full_metadata = {
                "key": query,
                "query": query_text,
                "prefix": prefix or "default",
                "cached_at": time.time(),
                "ttl": effective_ttl,
                "expires_at": time.time() + effective_ttl,
            }

            query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]

            if self._is_chromadb:
                document = json.dumps(value, default=str)
                self._collection.upsert(
                    ids=[query_hash],
                    embeddings=[query_embedding],
                    documents=[document],
                    metadatas=[full_metadata],
                )
            else:
                self._backend.add(
                    query=query_text,
                    embedding=query_embedding,
                    data=value,
                    metadata=full_metadata,
                )
            logger.info(
                f"SemanticCache síncrono INDEX: '{query_text[:50]}' [key={query}, ttl={effective_ttl}s]"
            )
        except Exception as e:
            logger.warning(f"SemanticCache.index falhou: {e}")

    def remove(self, query: str) -> None:
        """Remove query específica do cache de forma síncrona."""
        try:
            query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
            if self._is_chromadb:
                self._collection.delete(ids=[query_hash])
            else:
                self._backend.delete(query_hash)
            logger.info(f"SemanticCache REMOVE: '{query[:50]}'")
        except Exception as e:
            logger.warning(f"SemanticCache.remove falhou: {e}")


# ── Instância Global (Singleton) ─────────────────────────────────────────────
_semantic_cache_instance: SemanticCache | None = None


def get_semantic_cache(**kwargs) -> SemanticCache:
    """Obtém instância global do SemanticCache (singleton).

    Args:
        **kwargs: Argumentos para inicialização (apenas na primeira chamada).

    Returns:
        Instância global do SemanticCache.
    """
    global _semantic_cache_instance

    if _semantic_cache_instance is None:
        _semantic_cache_instance = SemanticCache(**kwargs)

    return _semantic_cache_instance
