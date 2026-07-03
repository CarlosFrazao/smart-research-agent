"""Grafo de conhecimento persistente para armazenar entidades e relacoes extraidas de pesquisas."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class KnowledgeGraph:
    def __init__(self, config: Any):
        self._config = config
        self._driver = None
        self._enabled = bool(getattr(config, "neo4j_uri", None))

    async def _get_driver(self) -> Any | None:
        if not self._enabled:
            return None
        if self._driver is None:
            try:
                from neo4j import AsyncGraphDatabase

                uri = self._config.neo4j_uri
                user = getattr(self._config, "neo4j_user", "neo4j")
                password = getattr(self._config, "neo4j_password", "password123")
                self._driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
                logger.info("KnowledgeGraph: Neo4j conectado com sucesso.")
            except ImportError:
                logger.warning(
                    "neo4j driver nao instalado. KnowledgeGraph desabilitado."
                )
                self._enabled = False
            except Exception as e:
                logger.warning(
                    f"Erro ao conectar com Neo4j: {e}. KnowledgeGraph desabilitado."
                )
                self._enabled = False
        return self._driver

    async def add_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        metadata: dict[str, Any] | None = None,
        source: str = "",
    ) -> bool:
        driver = await self._get_driver()
        if driver is None:
            return False
        try:
            async with driver.session() as session:
                query = (
                    "MERGE (s:Entity {name: }) "
                    "MERGE (o:Entity {name: }) "
                    "MERGE (s)-[r:RELATION {type: }]->(o) "
                    "SET r.source = , "
                    "    r.updated_at = datetime() "
                )
                params = {
                    "subject": subject.strip(),
                    "predicate": predicate.strip(),
                    "obj": obj.strip(),
                    "source": source,
                }
                if metadata:
                    for k, v in metadata.items():
                        query += f"SET r.{k} =  "
                        params[k] = v
                await session.run(query, **params)
            return True
        except Exception as e:
            logger.warning(f"KnowledgeGraph.add_fact falhou: {e}")
            return False

    async def query_entity(self, entity_name: str) -> list[dict[str, Any]]:
        driver = await self._get_driver()
        if driver is None:
            return []
        try:
            async with driver.session() as session:
                result = await session.run(
                    "MATCH (s:Entity {name: })-[r]->(o) "
                    "RETURN s.name AS subject, r.type AS predicate, o.name AS object, "
                    "       r.source AS source "
                    "LIMIT 50",
                    name=entity_name.strip(),
                )
                return [dict(record) async for record in result]
        except Exception as e:
            logger.warning(f"KnowledgeGraph.query_entity falhou: {e}")
            return []

    async def close(self) -> None:
        if self._driver:
            try:
                await self._driver.close()
            except Exception as e:
                logger.warning(f"Erro ao fechar o driver do Neo4j: {e}")
            finally:
                self._driver = None
