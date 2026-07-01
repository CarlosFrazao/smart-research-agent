"""
evidence_graph.py — Grafo de Evidências do SRA v5.0

Constrói um grafo semântico de claims extraídas dos resultados de pesquisa,
detecta relações CONFIRMS/CONTRADICTS via similaridade Jaccard e exporta
nos formatos Graphviz, D3.js e Cytoscape.js.
"""
import re
import uuid
import hashlib
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Any

from src.types import SearchResult

logger = logging.getLogger("evidence_graph")

# Limiar mínimo de sobreposição Jaccard para considerar duas claims relacionadas
_CONFIRM_THRESHOLD = 0.50
_CONTRADICT_THRESHOLD = 0.35

# Palavras de negação que sinalizam contradição
_NEGATION_WORDS = re.compile(
    r"\b(?:not|no|never|doesn't|don't|cannot|can't|isn't|aren't|wasn't|weren't"
    r"|não|nunca|jamais|nem|nenhum|nenhuma|sequer)\b",
    re.IGNORECASE,
)

# Sentinelas de afirmação positiva
_AFFIRMATION_WORDS = re.compile(
    r"\b(?:confirms|shows|proves|demonstrates|indicates|supports|validates"
    r"|confirma|demonstra|prova|indica|suporta|valida|evidencia)\b",
    re.IGNORECASE,
)


@dataclass
class Claim:
    id: str
    text: str
    source: str                      # URL da fonte
    source_name: str                 # Nome curto da fonte (ex: "arxiv")
    confidence: float = 0.0
    tokens: List[str] = field(default_factory=list)


@dataclass
class ClaimRelation:
    from_id: str
    to_id: str
    relation_type: str               # "CONFIRMS" | "CONTRADICTS"
    weight: float                    # Força da relação (0.0 - 1.0)


class EvidenceGraph:
    """
    Constrói e exporta grafos de evidências com suporte a múltiplos formatos.
    """

    def __init__(self, confirm_threshold: float = _CONFIRM_THRESHOLD,
                 contradict_threshold: float = _CONTRADICT_THRESHOLD):
        self.confirm_threshold = confirm_threshold
        self.contradict_threshold = contradict_threshold
        self.claims: List[Claim] = []
        self.relations: List[ClaimRelation] = []

    # ── Entry Point ───────────────────────────────────────────────────────────

    def build_from_results(self, results: List[SearchResult]) -> "EvidenceGraph":
        """
        Extrai claims de cada resultado e detecta relações entre elas.
        Retorna self para encadeamento.
        """
        self.claims = []
        self.relations = []

        for result in results:
            try:
                claims = self._extract_claims_from_text(
                    text=f"{result.title or ''} {result.description or ''}",
                    source=result.url or "",
                    source_name=result.source or "unknown",
                    confidence=getattr(result, "confidence_score", 0.0)
                )
                self.claims.extend(claims)
            except Exception as e:
                logger.warning(f"EvidenceGraph: falha ao extrair de {getattr(result, 'url', '')[:40]}: {e}")

        self.relations = self.detect_relations(self.claims)
        logger.info(
            f"EvidenceGraph: {len(self.claims)} claims, "
            f"{len(self.relations)} relações detectadas"
        )
        return self

    # ── Extração de Claims ────────────────────────────────────────────────────

    def _extract_claims_from_text(
        self,
        text: str,
        source: str,
        source_name: str,
        confidence: float,
    ) -> List[Claim]:
        """
        Segmenta o texto em frases candidatas a claim (comprimento entre 40 e 300 chars).
        """
        # Quebra por pontuação de fim de frase
        raw_sentences = re.split(r"(?<=[.!?])\s+", text)
        claims: List[Claim] = []

        for sentence in raw_sentences:
            sentence = sentence.strip()
            if len(sentence) < 40 or len(sentence) > 300:
                continue

            # Cria ID determinístico baseado no conteúdo + fonte
            raw_id = hashlib.md5(f"{source}:{sentence}".encode()).hexdigest()[:12]
            tokens = self._tokenize(sentence)

            claims.append(Claim(
                id=raw_id,
                text=sentence,
                source=source,
                source_name=source_name,
                confidence=confidence,
                tokens=tokens,
            ))

        return claims

    # ── Detecção de Relações ──────────────────────────────────────────────────

    def detect_relations(self, claims: List[Claim]) -> List[ClaimRelation]:
        """
        Compara pares de claims entre fontes diferentes.
        Detecta CONFIRMS (alta sobreposição, mesma polaridade) ou
        CONTRADICTS (sobreposição moderada + negação oposta).
        """
        relations: List[ClaimRelation] = []
        n = len(claims)

        for i in range(n):
            for j in range(i + 1, n):
                a = claims[i]
                b = claims[j]

                # Só compara claims de fontes diferentes para evitar auto-referência
                if a.source == b.source:
                    continue

                similarity = self._compute_similarity(a.tokens, b.tokens)

                if similarity < self.contradict_threshold:
                    continue

                # Determina se é CONFIRMS ou CONTRADICTS pela polaridade
                neg_a = bool(_NEGATION_WORDS.search(a.text))
                neg_b = bool(_NEGATION_WORDS.search(b.text))
                aff_a = bool(_AFFIRMATION_WORDS.search(a.text))
                aff_b = bool(_AFFIRMATION_WORDS.search(b.text))

                same_polarity = (neg_a == neg_b)

                if similarity >= self.confirm_threshold and same_polarity:
                    relations.append(ClaimRelation(
                        from_id=a.id,
                        to_id=b.id,
                        relation_type="CONFIRMS",
                        weight=round(similarity, 3),
                    ))
                elif not same_polarity:
                    relations.append(ClaimRelation(
                        from_id=a.id,
                        to_id=b.id,
                        relation_type="CONTRADICTS",
                        weight=round(similarity, 3),
                    ))

        return relations

    def _compute_similarity(self, tokens_a: List[str], tokens_b: List[str]) -> float:
        """
        Calcula similaridade Jaccard entre dois conjuntos de tokens.
        """
        if not tokens_a or not tokens_b:
            return 0.0
        set_a = set(tokens_a)
        set_b = set(tokens_b)
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union) if union else 0.0

    def _tokenize(self, text: str) -> List[str]:
        """
        Tokeniza o texto removendo stopwords e pontuação.
        """
        stopwords = {
            "a", "an", "the", "and", "or", "but", "in", "on", "at", "to",
            "for", "of", "with", "by", "from", "is", "are", "was", "were",
            "be", "been", "being", "have", "has", "had", "do", "does", "did",
            "will", "would", "could", "should", "may", "might",
            "o", "a", "os", "as", "e", "de", "da", "do", "em", "no", "na",
            "um", "uma", "com", "para", "por", "que", "se", "ao",
        }
        tokens = re.findall(r"\b[a-zA-ZÀ-ú]{3,}\b", text.lower())
        return [t for t in tokens if t not in stopwords]

    # ── Consulta de Conflitos ─────────────────────────────────────────────────

    def get_conflicting_claims(self) -> List[Tuple[Claim, Claim, float]]:
        """
        Retorna os pares de claims que se contradizem, ordenados por peso.
        """
        claim_map = {c.id: c for c in self.claims}
        conflicts = [
            (claim_map[r.from_id], claim_map[r.to_id], r.weight)
            for r in self.relations
            if r.relation_type == "CONTRADICTS"
            and r.from_id in claim_map and r.to_id in claim_map
        ]
        return sorted(conflicts, key=lambda x: x[2], reverse=True)

    # ── Exportadores ──────────────────────────────────────────────────────────

    def export_graphviz(self) -> str:
        """
        Exporta o grafo no formato DOT (Graphviz).
        """
        lines = ["digraph EvidenceGraph {", '  rankdir="LR";', '  node [shape=box, style=filled];']

        for c in self.claims:
            label = c.text[:60].replace('"', "'")
            color = "#a8d5a2" if c.confidence >= 0.7 else "#f4d06f" if c.confidence >= 0.5 else "#f4a261"
            lines.append(f'  "{c.id}" [label="{label}...", fillcolor="{color}"];')

        for r in self.relations:
            color = "#2d6a4f" if r.relation_type == "CONFIRMS" else "#c1121f"
            style = "solid" if r.relation_type == "CONFIRMS" else "dashed"
            lines.append(
                f'  "{r.from_id}" -> "{r.to_id}" '
                f'[color="{color}", style="{style}", label="{r.relation_type} ({r.weight:.2f})"];'
            )

        lines.append("}")
        return "\n".join(lines)

    def export_d3_json(self) -> Dict:
        """
        Exporta o grafo no formato JSON compatível com D3.js force-directed.
        """
        return {
            "nodes": [
                {
                    "id": c.id,
                    "label": c.text[:80],
                    "source": c.source_name,
                    "confidence": c.confidence,
                }
                for c in self.claims
            ],
            "links": [
                {
                    "source": r.from_id,
                    "target": r.to_id,
                    "type": r.relation_type,
                    "weight": r.weight,
                }
                for r in self.relations
            ],
        }

    def export_cytoscape_json(self) -> Dict:
        """
        Exporta o grafo no formato JSON compatível com Cytoscape.js.
        """
        elements: List[Dict] = []

        for c in self.claims:
            elements.append({
                "group": "nodes",
                "data": {
                    "id": c.id,
                    "label": c.text[:60],
                    "source": c.source_name,
                    "confidence": c.confidence,
                }
            })

        for r in self.relations:
            elements.append({
                "group": "edges",
                "data": {
                    "id": f"{r.from_id}-{r.to_id}",
                    "source": r.from_id,
                    "target": r.to_id,
                    "type": r.relation_type,
                    "weight": r.weight,
                }
            })

        return {"elements": elements}

    def summary(self) -> str:
        """
        Resumo legível do grafo para injeção no relatório.
        """
        confirms = sum(1 for r in self.relations if r.relation_type == "CONFIRMS")
        contradicts = sum(1 for r in self.relations if r.relation_type == "CONTRADICTS")

        if not self.claims:
            return ""

        lines = [
            "\n\n---\n",
            "## 🕸️ Grafo de Evidências\n",
            f"**Claims extraídas:** {len(self.claims)}  |  "
            f"**Confirmações cruzadas:** {confirms}  |  "
            f"**Contradições:** {contradicts}\n",
        ]

        conflicts = self.get_conflicting_claims()
        if conflicts:
            lines.append("### ⚡ Contradições Detectadas entre Fontes\n")
            lines.append("| Claim A | Claim B | Peso |")
            lines.append("| :--- | :--- | :--- |")
            for a, b, weight in conflicts[:5]:
                lines.append(
                    f"| [{a.source_name}] {a.text[:70]}… "
                    f"| [{b.source_name}] {b.text[:70]}… "
                    f"| {weight:.2f} |"
                )

        return "\n".join(lines)
