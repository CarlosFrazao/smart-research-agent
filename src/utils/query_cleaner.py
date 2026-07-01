import re
from typing import List

TECH_STOPWORDS = {
    "the", "a", "an", "is", "are", "what", "which", "best",
    "top", "vs", "versus",
}

TECH_DISAMBIGUATION = {
    "apple": ["apple_inc", "apple_fruit"],
    "java": ["java_language", "java_island", "java_coffee"],
    "go": ["go_language", "go_game"],
    "rust": ["rust_language", "rust_game"],
}


class QueryCleaner:
    @staticmethod
    def clean(query: str) -> str:
        query = query.lower().strip()
        query = re.sub(r"[^\w\s\-]", "", query)
        words = [w for w in query.split() if w not in TECH_STOPWORDS]
        return " ".join(words)

    @staticmethod
    def disambiguate(query: str) -> List[str]:
        words = query.lower().split()
        variants = []
        for word in words:
            if word in TECH_DISAMBIGUATION:
                variants.extend(TECH_DISAMBIGUATION[word])
        return variants if variants else [query]
