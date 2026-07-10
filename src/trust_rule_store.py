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
from typing import List, Dict

logger = logging.getLogger(__name__)

VALID_TIERS = {"allow", "deny"}


class TrustRuleStore:
    def __init__(self, store_path: str | None = None):
        """
        Initialize the TrustRuleStore.

        Args:
            store_path: Path to the JSONL file. If None, uses
                TRUST_RULE_STORE_PATH env var or defaults to
                reports/_trust_rules.jsonl.
        """
        self.path = Path(
            store_path
            or os.environ.get(
                "TRUST_RULE_STORE_PATH", str(Path("reports") / "_trust_rules.jsonl")
            )
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, user_id: str, source: str, tier: str) -> dict:
        """
        Record (or update) a trust rule entry.

        Args:
            user_id: Identifier of the user.
            source: Source name (e.g., searcher identifier).
            tier: Trust tier - must be "allow" or "deny".

        Returns:
            The entry dictionary that was recorded.

        Raises:
            ValueError: If user_id is empty or tier is not valid.
        """
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
        logger.info(
            "Regra de confiança gravada: user=%s source=%s -> %s", user_id, source, tier
        )
        return entry

    def load_all(self) -> List[dict]:
        """
        Load all stored trust rule entries.

        Returns:
            List of entry dictionaries. Empty list if file does not exist.
        """
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
                    logger.warning("Skipping invalid JSON line: %s", line[:80])
        return records

    def get_rules_for_user(self, user_id: str) -> Dict[str, str]:
        """
        Get the latest trust tier for each source for a given user.

        Rules are resolved by timestamp; the latest entry for a source wins.

        Args:
            user_id: Identifier of the user.

        Returns:
            Dict mapping source names to their latest tier.
        """
        user_entries = [
            entry for entry in self.load_all() if entry.get("user_id") == user_id
        ]

        # Map source -> (index, timestamp, tier) keeping the latest entry
        # by insertion order (append preserves chronological order)
        latest: Dict[str, tuple[int, str, str]] = {}
        for idx, entry in enumerate(user_entries):
            src = entry.get("source", "")
            ts = entry.get("timestamp", "")
            tier = entry.get("tier", "")
            if src and (src not in latest or idx > latest[src][0]):
                latest[src] = (idx, ts, tier)

        # Return only source -> tier mapping
        return {src: tier for src, (_, _, tier) in latest.items()}

    def clear(self, user_id: str | None = None) -> int:
        """
        Clear trust rules.

        Args:
            user_id: If provided, only that user's rules are cleared.
                If None, all rules are cleared.

        Returns:
            Number of rules removed.
        """
        if self.path.exists():
            current_entries = self.load_all()
            if user_id is None:
                self.path.unlink()
                return len(current_entries)
            # Filter out entries for the specified user
            remaining_entries = [
                entry for entry in current_entries if entry.get("user_id") != user_id
            ]
            # Rewrite the file with remaining entries
            with open(self.path, "w", encoding="utf-8") as f:
                for entry in remaining_entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return len(current_entries) - len(remaining_entries)
        return 0
