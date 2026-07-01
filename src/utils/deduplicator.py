import re
from typing import List
from difflib import SequenceMatcher


class Deduplicator:
    SIMILARITY_THRESHOLD = 0.75

    @staticmethod
    def normalize_url(url: str) -> str:
        url = url.lower().strip().rstrip("/")
        url = re.sub(r"\?.*$", "", url)
        url = re.sub(r"#.*$", "", url)
        url = re.sub(r"^https?://", "", url)
        url = re.sub(r"^www\.", "", url)
        return url

    @staticmethod
    def extract_entity(title: str) -> str:
        title = title.lower()
        title = re.sub(r"^(show hn|ask hn|tell hn):\s*", "", title)
        words = title.split()
        if not words:
            return ""
        if "/" in words[0]:
            return words[0].split("/")[-1]
        return words[0]

    @classmethod
    def is_duplicate(cls, result1, result2) -> bool:
        if cls.normalize_url(result1.url) == cls.normalize_url(result2.url):
            return True

        entity1 = cls.extract_entity(result1.title)
        entity2 = cls.extract_entity(result2.title)
        if entity1 and entity2 and entity1 == entity2:
            return True

        title_sim = SequenceMatcher(
            None, result1.title.lower(), result2.title.lower()
        ).ratio()
        if title_sim > cls.SIMILARITY_THRESHOLD:
            return True

        if result1.source == result2.source:
            desc_sim = SequenceMatcher(
                None, result1.description[:200], result2.description[:200]
            ).ratio()
            if desc_sim > cls.SIMILARITY_THRESHOLD:
                return True

        return False

    @classmethod
    def deduplicate(cls, results: List) -> List:
        unique = []
        for r in results:
            if not any(cls.is_duplicate(r, u) for u in unique):
                unique.append(r)
        return unique
