"""
migrate_memory_v1_to_v2.py — Script de Migração OrvixMemory v1 → OrvixMemoryV2

Lê todos os registros do SQLite v1 (memories + entities + entity_links) e os
reindexia no OrvixMemoryV2 (SQLite v2 + ChromaDB + KuzuDB), preservando
metadados e relacionamentos de entidades.

Uso:
    python migrate_memory_v1_to_v2.py [--v1-db PATH] [--v2-db PATH] [--dry-run]

Segurança:
    - NÃO modifica o banco v1 (somente leitura).
    - Cria um backup .bak do banco v1 antes de iniciar.
    - Em caso de erro parcial, registra IDs não migrados em migration_errors.log.
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("migration_v1_to_v2")

# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_V1_DB = os.environ.get("RESEARCH_MEMORY_DB", "reports/.research_memory.db")
DEFAULT_V2_DB = os.environ.get("RESEARCH_MEMORY_DB_V2", "reports/.research_memory_v2.db")
DEFAULT_KUZU  = os.environ.get("KUZU_DATA_PATH", "kuzu_data")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _connect_v1(v1_db: str) -> sqlite3.Connection:
    """Abre o banco v1 em modo SOMENTE LEITURA."""
    uri = f"file:{Path(v1_db).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _backup_v1(v1_db: str) -> str:
    """Cria uma cópia .bak do banco v1 antes de qualquer operação."""
    src = Path(v1_db)
    if not src.exists():
        raise FileNotFoundError(f"Banco v1 não encontrado: {v1_db}")
    bak = src.with_suffix(f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(src, bak)
    logger.info(f"Backup criado: {bak}")
    return str(bak)


def _load_v1_records(conn: sqlite3.Connection) -> list[dict]:
    """
    Carrega todos os registros da v1 com seus metadados e entidades associadas.
    Retorna lista de dicts prontos para inserção na v2.
    """
    rows = conn.execute(
        "SELECT id, content, metadata, created_at FROM memories ORDER BY id"
    ).fetchall()

    records = []
    for row in rows:
        mem_id = row["id"]
        content = row["content"]
        created_at = row["created_at"]

        # Carrega metadados da v1
        try:
            meta = json.loads(row["metadata"] or "{}")
        except json.JSONDecodeError:
            meta = {}

        # Injeta marcador de migração e data original
        meta["_migrated_from_v1"] = True
        meta["_v1_id"] = mem_id
        meta["_v1_created_at"] = created_at

        # Carrega entidades associadas a esta memória (via entity_links)
        entity_rows = conn.execute(
            """
            SELECT e.name
            FROM entity_links el
            JOIN entities e ON e.id = el.entity_id
            WHERE el.memory_id = ?
            """,
            (mem_id,),
        ).fetchall()
        meta["_v1_entities"] = [r["name"] for r in entity_rows]

        records.append({"content": content, "metadata": meta})

    return records


def _run_migration(
    v1_db: str,
    v2_db: str,
    kuzu_path: str,
    dry_run: bool = False,
) -> dict:
    """
    Executa a migração completa.
    Retorna relatório com counts de sucesso/erro.
    """
    from src.memory.orvix_memory_v2 import OrvixMemoryV2

    # 1. Backup obrigatório
    bak = _backup_v1(v1_db)

    # 2. Lê registros da v1
    v1_conn = _connect_v1(v1_db)
    total_v1 = v1_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    logger.info(f"Total de registros na v1: {total_v1}")

    records = _load_v1_records(v1_conn)
    v1_conn.close()

    if dry_run:
        logger.info(f"[DRY-RUN] Migração simulada: {len(records)} registros seriam migrados.")
        return {"total": len(records), "migrated": 0, "errors": 0, "dry_run": True}

    # 3. Instancia a v2 com paths explícitos
    memory_v2 = OrvixMemoryV2(db_path=v2_db, kuzu_path=kuzu_path)

    migrated = 0
    errors = []
    error_log_path = Path("migration_errors.log")

    for record in records:
        try:
            memory_v2.add(
                content=record["content"],
                metadata=record["metadata"],
            )
            migrated += 1
            if migrated % 50 == 0:
                logger.info(f"Progresso: {migrated}/{len(records)} registros migrados...")
        except Exception as e:
            v1_id = record["metadata"].get("_v1_id", "?")
            msg = f"ERRO ao migrar v1_id={v1_id}: {e}"
            logger.error(msg)
            errors.append(msg)

    # 4. Salva log de erros se houver
    if errors:
        with open(error_log_path, "w", encoding="utf-8") as f:
            f.write(f"Migration errors — {datetime.now().isoformat()}\n")
            f.write(f"Total errors: {len(errors)}\n\n")
            f.write("\n".join(errors))
        logger.warning(f"{len(errors)} erros registrados em {error_log_path}")

    # 5. Fecha conexões
    memory_v2.close()

    report = {
        "total_v1": total_v1,
        "migrated": migrated,
        "errors": len(errors),
        "dry_run": False,
        "backup": bak,
        "v2_db": v2_db,
        "kuzu_path": kuzu_path,
    }
    return report


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migração OrvixMemory v1 (SQLite puro) → OrvixMemoryV2 (RAG Híbrido)"
    )
    parser.add_argument("--v1-db",    default=DEFAULT_V1_DB,  help="Caminho do banco SQLite v1")
    parser.add_argument("--v2-db",    default=DEFAULT_V2_DB,  help="Caminho destino do banco SQLite v2")
    parser.add_argument("--kuzu-dir", default=DEFAULT_KUZU,   help="Diretório do KuzuDB v2")
    parser.add_argument("--dry-run",  action="store_true",    help="Simula a migração sem escrever")
    args = parser.parse_args()

    if not Path(args.v1_db).exists():
        logger.error(f"Banco v1 não encontrado em: {args.v1_db}")
        logger.info("Use --v1-db para especificar o caminho correto.")
        return

    logger.info("=" * 60)
    logger.info("OrvixMemory v1 → v2 Migration")
    logger.info(f"  v1: {args.v1_db}")
    logger.info(f"  v2: {args.v2_db}")
    logger.info(f"  kuzu: {args.kuzu_dir}")
    logger.info(f"  dry-run: {args.dry_run}")
    logger.info("=" * 60)

    report = _run_migration(
        v1_db=args.v1_db,
        v2_db=args.v2_db,
        kuzu_path=args.kuzu_dir,
        dry_run=args.dry_run,
    )

    logger.info("=" * 60)
    logger.info("RELATÓRIO FINAL:")
    for k, v in report.items():
        logger.info(f"  {k}: {v}")
    logger.info("=" * 60)

    if not report.get("dry_run") and report.get("errors", 0) == 0:
        logger.info("✅ Migração concluída com SUCESSO — zero erros.")
    elif report.get("errors", 0) > 0:
        logger.warning(f"⚠️  Migração concluída com {report['errors']} erro(s). Verifique migration_errors.log")


if __name__ == "__main__":
    main()
