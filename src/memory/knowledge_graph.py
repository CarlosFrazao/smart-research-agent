"""
knowledge_graph.py — Grafo de Conhecimento unificado via KuzuDB (Fase 0 — Dívida Técnica).

Mantém a API pública original (add_fact, query_entity, close) mas opera sobre
o KuzuDB local em vez do Neo4j remoto, eliminando a dependência de servidor externo
e unificando os dois backends de grafo do SRA numa única base de dados local.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from src.knowledge_graph import SemanticKnowledgeGraph, Triple

logger = logging.getLogger(__name__)


class KnowledgeGraph(SemanticKnowledgeGraph):
    """
    Wrapper compatível com a interface legada de KnowledgeGraph (Neo4j),
    mas operando sobre o KuzuDB como backend unificado do SRA.

    Mantém os atributos públicos `_enabled`, `_driver` e `kuzu_conn` para
    retrocompatibilidade com os testes existentes em test_bloco8_arquitetura.py.
    """

    def __init__(self, config: Any, kuzu_conn: Optional[Any] = None):
        """
        Inicializa a conexão KuzuDB.

        O caminho do banco de dados é resolvido nesta ordem:
        1. `config.kuzu_data_path` (atributo do objeto Config)
        2. Variável de ambiente `KUZU_DATA_PATH`
        3. Fallback para o diretório local `kuzu_data/`
        """
        self._config = config
        self._enabled = True
        self._driver = None  # mantido para retrocompatibilidade com testes

        # Se uma conexão KuzuDB já existente foi passada (ex.: criada pelo
        # OrvixMemory), reaproveitá-la em vez de abrir o banco novamente.
        # Isso evita a dupla abertura do mesmo arquivo kuzu.db no mesmo
        # processo, que causava o lock "está bloqueado após múltiplas
        # tentativas" no Windows.
        if kuzu_conn is not None:
            self.kuzu_conn = kuzu_conn
            self.kuzu_db = getattr(kuzu_conn, "_db", None)
            super().__init__(kuzu_conn=self.kuzu_conn, llm_client=None)
            logger.info(
                "KnowledgeGraph (KuzuDB): conexão reutilizada de '%s'.",
                getattr(config, "kuzu_data_path", "kuzu_data"),
            )
            return

        # Resolver o caminho do banco de dados
        kuzu_path = getattr(config, "kuzu_data_path", None) or os.environ.get(
            "KUZU_DATA_PATH", "kuzu_data"
        )
        db_dir = Path(kuzu_path)
        db_dir.mkdir(parents=True, exist_ok=True)

        # Usar um locking file para evitar concorrência entre processos/hthreads
        # para o mesmo banco de dados KuzuDB (cross-platform)
        lock_file = db_dir / "kuzu.lock"
        lock_acquired = False

        # Tentar adquirir lock sem bloquear por muito tempo
        try:
            # Import condicional para compatibilidade com Windows
            import fcntl

            # Linux/macOS style file locking
            with open(lock_file, "w") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_acquired = True

                    # Lock adquirido, prosseguir com inicialização
                    import kuzu

                    self.kuzu_db = kuzu.Database(str(db_dir / "kuzu.db"))
                    self.kuzu_conn = kuzu.Connection(self.kuzu_db)
                    super().__init__(kuzu_conn=self.kuzu_conn, llm_client=None)
                    logger.info(
                        "KnowledgeGraph (KuzuDB): conexão estabelecida em '%s'.",
                        db_dir / "kuzu.db",
                    )
                except (OSError, IOError):
                    # Lock não pode ser adquirido imediatamente
                    if self.kuzu_conn:
                        # Já existe uma conexão ativa do mesmo processo/-thread
                        # Podemos prosseguir com a conexão existente
                        logger.debug(
                            "KnowledgeGraph (KuzuDB): compartilhando conexão existente de %s.",
                            db_dir / "kuzu.db",
                        )
                        super().__init__(kuzu_conn=self.kuzu_conn, llm_client=None)
                    else:
                        # Nenhuma conexão ativa, aguarda lock ou falha
                        logger.warning(
                            "KnowledgeGraph (KuzuDB): banco de dados %s está bloqueado. "
                            "Operando em modo desabilitado.",
                            db_dir / "kuzu.db",
                        )
                        super().__init__(kuzu_conn=None, llm_client=None)
                        self._enabled = False
        except ImportError:
            # Windows fallback - usar um arquivo de lock simples com polling
            import time
            import msvcrt

            max_attempts = 10
            attempt = 0

            while attempt < max_attempts:
                try:
                    # Tentar abrir o arquivo de lock em modo exclusivo
                    with open(lock_file, "x") as f:
                        # Arquivo criado com sucesso, temos o lock
                        lock_acquired = True
                        break
                except FileExistsError:
                    # Arquivo já existe, aguardar um pouco e tentar novamente
                    time.sleep(0.1)
                    attempt += 1
                except Exception:
                    # Outros erros, sair do loop
                    break

            if lock_acquired:
                try:
                    # Lock adquirido, prosseguir com inicialização
                    import kuzu

                    self.kuzu_db = kuzu.Database(str(db_dir / "kuzu.db"))
                    self.kuzu_conn = kuzu.Connection(self.kuzu_db)
                    super().__init__(kuzu_conn=self.kuzu_conn, llm_client=None)
                    logger.info(
                        "KnowledgeGraph (KuzuDB): conexão estabelecida em '%s'.",
                        db_dir / "kuzu.db",
                    )
                except Exception as e:
                    # kuzu indisponível ou falha ao abrir o banco após adquirir o
                    # lock — degrada graciosamente em vez de propagar, mantendo a
                    # instância em modo desabilitado (comportamento espelhado do
                    # ramo fcntl).
                    logger.warning(
                        "KnowledgeGraph (KuzuDB): falha ao inicializar %s: %s. "
                        "Operando em modo desabilitado.",
                        db_dir / "kuzu.db",
                        e,
                    )
                    self.kuzu_conn = None
                    super().__init__(kuzu_conn=None, llm_client=None)
                    self._enabled = False
                finally:
                    # Liberar lock removendo o arquivo
                    try:
                        lock_file.unlink()
                    except Exception:
                        pass
            else:
                # Não foi possível obter lock após várias tentativas
                logger.warning(
                    "KnowledgeGraph (KuzuDB): banco de dados %s está bloqueado após múltiplas tentativas. "
                    "Operando em modo desabilitado.",
                    db_dir / "kuzu.db",
                )
                super().__init__(kuzu_conn=None, llm_client=None)
                self._enabled = False
        finally:
            # Garantir liberação de recursos
            pass

    # ── API Pública (retrocompatível com o código legado) ─────────────────────

    async def add_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        metadata: dict[str, Any] | None = None,
        source: str = "",
    ) -> bool:
        """
        Adiciona uma tripla semântica ao KuzuDB.

        Traduz a interface legada (subject, predicate, object) para Triple
        do SemanticKnowledgeGraph e delega para `add_triple`.

        Returns:
            True se a operação foi bem-sucedida, False caso contrário.
        """
        if not self._enabled or not self.kuzu_conn:
            return False
        try:
            triple = Triple(
                subject=subject.strip(),
                relation=predicate.strip(),
                object=obj.strip(),
                confidence=float(metadata.get("confidence", 0.75))
                if metadata
                else 0.75,
                source=source or "add_fact",
            )
            self.add_triple(triple)
            return True
        except Exception as e:
            logger.warning("KnowledgeGraph.add_fact (KuzuDB) falhou: %s", e)
            return False

    async def query_entity(self, entity_name: str) -> list[dict[str, Any]]:
        """
        Consulta todas as triplas cujo sujeito seja `entity_name`.

        Traduz os objetos Triple retornados por `query_graph` de volta ao
        formato de dicionário esperado pelo código legado.

        Returns:
            Lista de dicts com chaves: subject, predicate, object, source.
            Lista vazia se desabilitado ou em caso de erro.
        """
        if not self._enabled or not self.kuzu_conn:
            return []
        try:
            triples = self.query_graph(subject=entity_name)
            return [
                {
                    "subject": t.subject,
                    "predicate": t.relation,
                    "object": t.object,
                    "source": t.source,
                }
                for t in triples
            ]
        except Exception as e:
            logger.warning("KnowledgeGraph.query_entity (KuzuDB) falhou: %s", e)
            return []

    async def _get_driver(self) -> None:
        """
        Stub mantido para retrocompatibilidade com os testes do bloco 8.
        No backend KuzuDB não existe um 'driver' lazy — a conexão é feita
        no __init__. Este método retorna None e desabilita a instância se
        o kuzu_conn não estiver ativo.
        """
        if not self.kuzu_conn:
            self._enabled = False
        return None

    async def close(self) -> None:
        """Encerra a conexão KuzuDB e marca a instância como desabilitada."""
        self.kuzu_conn = None
        self._driver = None
        if hasattr(self, "kuzu_db"):
            self.kuzu_db = None
        self._enabled = False
        logger.info("KnowledgeGraph (KuzuDB): conexão encerrada.")
