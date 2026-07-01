import re
import logging
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple

from src.types import SearchResult, ExpandedQuery, IntentResult, Domain, Intention

logger = logging.getLogger("conflict_detector")

_NUMBER_PATTERNS = [
    r"(\d+(?:[\.,]\d+)?)\s*%",                          # porcentagens: "cresceu 18%"
    r"(?:R\$|US\$|\$|€)\s*(\d+(?:[\.,]\d+)?[KkMmBb]?)",  # monetário: "US$ 1.2B"
    r"(\d+(?:[\.,]\d+)?)\s*(?:milhões?|bilhões?|mil)",   # contagens: "14 mil usuários"
    r"(\d+(?:[\.,]\d+)?)\s*(?:req\/s|ms|rpm|rps)",       # taxas técnicas
]
_DEFAULT_DIVERGENCE_THRESHOLD = 0.20


@dataclass
class NumericClaim:
    value: float
    unit: str
    context: str
    metric_name: str
    source: str
    source_name: str
    confidence: float


@dataclass
class Conflict:
    metric_name: str
    claims: List[NumericClaim]
    divergence_ratio: float       # abs(max-min)/min
    severity: str                 # critical | high | medium | low
    resolution_query: str


@dataclass
class ConflictReport:
    total_claims_extracted: int
    conflicts: List[Conflict]
    critical_conflicts: List[Conflict]

    @property
    def has_critical(self) -> bool:
        return len(self.critical_conflicts) > 0

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts)


class ConflictDetector:
    def __init__(self, llm_client=None, divergence_threshold: float = _DEFAULT_DIVERGENCE_THRESHOLD):
        self.llm = llm_client
        self.divergence_threshold = divergence_threshold
        self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in _NUMBER_PATTERNS]

    def detect(self, results: List[SearchResult]) -> ConflictReport:
        """
        Extrai e analisa claims numéricas em busca de divergências estatísticas.
        """
        all_claims: List[NumericClaim] = []
        for r in results:
            try:
                claims = self._extract_numeric_claims(r)
                all_claims.extend(claims)
            except Exception as e:
                logger.warning(f"ConflictDetector: falha ao extrair de {r.url[:40]}: {e}")

        # Agrupa claims por (metric_name, unit)
        groups: Dict[Tuple[str, str], List[NumericClaim]] = {}
        for claim in all_claims:
            key = (claim.metric_name, claim.unit)
            groups.setdefault(key, []).append(claim)

        conflicts: List[Conflict] = []
        critical_conflicts: List[Conflict] = []

        for (metric_name, unit), group_claims in groups.items():
            # Filtra claims repetidas da mesma URL no mesmo grupo para evitar ruído
            unique_sources = {}
            for c in group_claims:
                if c.source not in unique_sources or c.confidence > unique_sources[c.source].confidence:
                    unique_sources[c.source] = c
            
            filtered_claims = list(unique_sources.values())
            if len(filtered_claims) < 2:
                continue

            conflict = self._analyze_group(metric_name, filtered_claims)
            if conflict:
                conflicts.append(conflict)
                if conflict.severity == "critical":
                    critical_conflicts.append(conflict)

        return ConflictReport(
            total_claims_extracted=len(all_claims),
            conflicts=conflicts,
            critical_conflicts=critical_conflicts
        )

    def _extract_numeric_claims(self, result: SearchResult) -> List[NumericClaim]:
        """
        Varre o título e a descrição do resultado extraindo números no padrão.
        """
        claims: List[NumericClaim] = []
        text = f"{result.title or ''} {result.description or ''}"
        
        # Divide em sentenças
        sentences = re.split(r"[.!?]\s+", text)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            for pattern in self._compiled_patterns:
                for match in pattern.finditer(sentence):
                    matched_str = match.group(0)
                    val_str = match.group(1)
                    
                    try:
                        # Normaliza pontuação numérica
                        clean_val = val_str.replace(",", ".")
                        
                        # Verifica multiplicadores k/m/b colados
                        multiplier = 1.0
                        if clean_val and clean_val[-1].lower() in ["k", "m", "b"]:
                            char = clean_val[-1].lower()
                            clean_val = clean_val[:-1]
                            if char == "k":
                               multiplier = 1000.0
                            elif char == "m":
                               multiplier = 1000000.0
                            elif char == "b":
                               multiplier = 1000000000.0
                        
                        value = float(clean_val) * multiplier
                    except Exception:
                        continue
                    
                    unit = matched_str.replace(val_str, "").strip()
                    if not unit and "%" in matched_str:
                        unit = "%"
                        
                    start_idx = match.start()
                    words_before = sentence[:start_idx].split()
                    context_window = " ".join(words_before[-4:]) if words_before else ""
                    metric_name = self._normalize_metric_name(context_window)
                    
                    if not metric_name:
                        continue
                        
                    claims.append(NumericClaim(
                        value=value,
                        unit=unit,
                        context=sentence[:200],  # Limita tamanho do contexto
                        metric_name=metric_name,
                        source=result.url or "",
                        source_name=result.source or "unknown",
                        confidence=getattr(result, "confidence_score", 0.0)
                    ))
        return claims

    def _normalize_metric_name(self, text: str) -> str:
        """
        Normaliza e limpa as palavras de contexto para agrupamento.
        """
        text = text.lower()
        text = re.sub(r"[^\w\s]", "", text)
        stopwords = {
            "de", "da", "do", "em", "no", "na", "o", "a", "os", "as", 
            "um", "uma", "com", "para", "por", "que", "se", "ao", "aos",
            "the", "of", "and", "in", "to", "for", "with", "on", "at", "by"
        }
        words = [w for w in text.split() if w not in stopwords]
        # Mantém até as últimas 3 palavras significativas
        return " ".join(words[-3:])

    def _analyze_group(self, metric_name: str, claims: List[NumericClaim]) -> Optional[Conflict]:
        """
        Calcula a divergência entre claims do mesmo grupo.
        """
        vals = [c.value for c in claims]
        min_val = min(vals)
        max_val = max(vals)
        
        if min_val == 0.0:
            divergence = max_val
        else:
            divergence = (max_val - min_val) / min_val

        if divergence < self.divergence_threshold:
            return None

        # Determina a severidade
        if divergence >= 1.0:
            severity = "critical"
        elif divergence >= 0.50:
            severity = "high"
        elif divergence >= 0.30:
            severity = "medium"
        else:
            severity = "low"

        resolution_query = self._generate_resolution_query(metric_name, claims)

        return Conflict(
            metric_name=metric_name,
            claims=claims,
            divergence_ratio=divergence,
            severity=severity,
            resolution_query=resolution_query
        )

    def _generate_resolution_query(self, metric_name: str, claims: List[NumericClaim]) -> str:
        """
        Gera uma query focada baseada nos valores divergentes.
        """
        # Extrai os nomes das fontes ou domínios
        source_names = list(set(c.source_name for c in claims))
        query = f"official statistics {metric_name} "
        if claims[0].unit:
            query += f"in {claims[0].unit} "
        query += " ".join(source_names)
        return query.strip()

    async def resolve(self, report: ConflictReport, orchestrator, max_conflicts: int = 3) -> List[SearchResult]:
        """
        Executa buscas focadas para resolver os conflitos críticos detectados.
        """
        new_results: List[SearchResult] = []
        conflicts_to_resolve = report.critical_conflicts[:max_conflicts]
        
        for conflict in conflicts_to_resolve:
            query = conflict.resolution_query
            logger.info(f"ConflictDetector: resolvendo conflito para '{conflict.metric_name}' com query: '{query}'")
            
            try:
                expanded = [
                    ExpandedQuery(
                        query=query,
                        type="fact_check",
                        priority="alta",
                        rationale=f"resolve conflict: {conflict.metric_name}"
                    )
                ]
                intent = IntentResult(
                    domain=Domain.GENERAL,
                    entities=[],
                    intention=Intention.EVALUATE,
                    urgency="nao",
                    confidence="alta"
                )
                
                source_plan = orchestrator.source_planner.plan(intent, expanded)
                results = await orchestrator._parallel_search(expanded, source_plan, intent)
                new_results.extend(results[:5])
            except Exception as e:
                logger.warning(f"ConflictDetector: falha ao buscar resolução de conflito: {e}")
                
        return new_results

    def format_conflicts_for_report(self, report: ConflictReport) -> str:
        """
        Formata o relatório de conflitos em Markdown para exibição.
        """
        if not report.conflicts:
            return ""

        lines = [
            "\n\n---\n",
            "## ⚠️ Conflitos Detectados nas Fontes\n",
            "Foi detectada divergência de dados estatísticos/numéricos entre as fontes citadas.\n",
            "| Métrica | Valores Encontrados | Divergência | Severidade |",
            "| :--- | :--- | :--- | :--- |"
        ]

        for c in report.conflicts:
            severity_emojis = {
                "critical": "🔴 critical",
                "high": "🟠 high",
                "medium": "🟡 medium",
                "low": "🟢 low"
            }
            sev = severity_emojis.get(c.severity, c.severity)
            
            # Formata lista de valores e fontes
            val_sources = []
            for claim in c.claims:
                formatted_val = f"{claim.value:g}"
                if claim.unit == "%":
                    formatted_val += "%"
                elif claim.unit:
                    formatted_val += f" {claim.unit}"
                val_sources.append(f"{formatted_val} ({claim.source_name})")
            
            vals_str = " vs ".join(val_sources)
            lines.append(f"| {c.metric_name} | {vals_str} | {c.divergence_ratio:.0%} | {sev} |")

        return "\n".join(lines)
