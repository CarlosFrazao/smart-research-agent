import re
import logging

logger = logging.getLogger(__name__)

class QueryValidator:
    INVALID_PATTERNS = [
        r"> Gerado em",
        r"\n\n",
        r"## ",
        r"---",
        r"\|",
        r"\[.*\]\(.*\)",  # Markdown links
    ]

    @classmethod
    def is_valid(cls, query: str) -> bool:
        """Retorna True se a query for válida (comprimento aceitável e sem padrões suspeitos)."""
        if not query:
            return False
            
        query_stripped = query.strip()
        if len(query_stripped) < 3 or len(query_stripped) > 200:
            return False

        # Verifica padrões suspeitos/malformados
        for pattern in cls.INVALID_PATTERNS:
            if re.search(pattern, query_stripped, re.IGNORECASE):
                return False
                
        return True

    @classmethod
    def sanitize(cls, query: str) -> str:
        """Remove espaços excessivos e trunca a query se necessário."""
        if not query:
            return ""
        # Remove quebras de linha e tabs
        sanitized = re.sub(r"[\r\n\t]+", " ", query)
        # Remove múltiplos espaços consecutivos
        sanitized = re.sub(r"\s+", " ", sanitized)
        sanitized = sanitized.strip()
        
        # Trunca a 200 caracteres se passar do limite
        if len(sanitized) > 200:
            sanitized = sanitized[:200].strip()
            
        return sanitized
