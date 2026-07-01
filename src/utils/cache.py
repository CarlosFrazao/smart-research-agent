import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Any
from pathlib import Path


class Cache:
    def __init__(self, cache_dir: str = "./.cache", ttl_hours: int = 24):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=ttl_hours)

    def _key(self, prefix: str, query: str) -> str:
        hash_key = hashlib.md5(f"{prefix}:{query}".encode()).hexdigest()
        return f"{prefix}_{hash_key}.json"

    def get(self, prefix: str, query: str) -> Optional[Any]:
        path = self.cache_dir / self._key(prefix, query)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data["_cached_at"])
        if datetime.now() - cached_at > self.ttl:
            path.unlink()
            return None
        return data["value"]

    def set(self, prefix: str, query: str, value: Any) -> None:
        path = self.cache_dir / self._key(prefix, query)
        data = {"_cached_at": datetime.now().isoformat(), "value": value}
        path.write_text(json.dumps(data, default=str, indent=2), encoding="utf-8")

    def invalidate(self, prefix: str) -> None:
        for f in self.cache_dir.glob(f"{prefix}_*.json"):
            f.unlink()
