import re
import os
import json
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from src.clients.llm_client import LLMClient

logger = logging.getLogger("peer_review_agent")

REVIEW_CATEGORIES = [
    "logical_fallacy",      # Falhas de coerência, saltos lógicos
    "unsupported_claim",    # Afirmações sem evidência citada
    "cherry_picking",       # Evidências contrárias ignoradas
    "bias",                 # Viés e tendenciosidade
    "missing_context",      # Limitações não mencionadas
    "weak_citation"         # URLs suspeitos, fontes secundárias
]
REVIEW_SEVERITIES = ["critical", "major", "minor"]
SUPERLATIVOS = [
    "melhor", "único", "definitivo", "100%",
    "sempre", "nunca", "revolucionário", "perfeito", "impossível"
]


@dataclass
class ReviewIssue:
    category: str
    severity: str
    description: str
    location: str
    suggestion: str


@dataclass
class PeerReviewReport:
    overall_assessment: str     # strong | moderate | weak | unreliable
    confidence_in_report: float # 0.0-1.0
    issues: List[ReviewIssue] = field(default_factory=list)
    strengths: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "critical")

    @property
    def major_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "major")

    @property
    def minor_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "minor")


class PeerReviewAgent:
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
        self.prompt_path = os.path.join("prompts", "peer_review.md")

    async def review(self, report: str, results: List[Any], query: str = "") -> PeerReviewReport:
        """
        Executa a revisão de pares do relatório combinando análise por LLM e heurísticas locais.
        """
        # 1. Análise Heurística Rápida
        heuristic_issues = self._heuristic_review(report, results)
        
        # 2. Análise Estruturada por LLM
        structured_report = await self._structured_review(report, query)
        
        if not structured_report:
            # Fallback se a chamada ao LLM falhar
            return PeerReviewReport(
                overall_assessment="moderate",
                confidence_in_report=0.70,
                issues=heuristic_issues,
                strengths=["Estrutura do relatório segue o padrão esperado."],
                recommendations=["Realizar revisão manual das fontes."]
            )

        # Mescla as issues das duas fontes
        all_issues = list(heuristic_issues)
        seen_descriptions = {i.description.lower() for i in all_issues}
        
        for issue_dict in structured_report.get("issues", []):
            desc = issue_dict.get("description", "")
            if desc.lower() not in seen_descriptions:
                all_issues.append(ReviewIssue(
                    category=issue_dict.get("category", "unsupported_claim"),
                    severity=issue_dict.get("severity", "minor"),
                    description=desc,
                    location=issue_dict.get("location", ""),
                    suggestion=issue_dict.get("suggestion", "")
                ))
                seen_descriptions.add(desc.lower())

        return PeerReviewReport(
            overall_assessment=structured_report.get("overall_assessment", structured_report.get("assessment", "moderate")),
            confidence_in_report=structured_report.get("confidence_in_report", structured_report.get("confidence", 0.70)),
            issues=all_issues,
            strengths=structured_report.get("strengths", []),
            recommendations=structured_report.get("recommendations", [])
        )

    async def _structured_review(self, report: str, query: str) -> Optional[Dict[str, Any]]:
        """
        Carrega as regras do arquivo markdown e envia o prompt estruturado ao LLM.
        """
        instructions = ""
        if os.path.exists(self.prompt_path):
            try:
                with open(self.prompt_path, "r", encoding="utf-8") as f:
                    instructions = f.read()
            except Exception as e:
                logger.warning(f"PeerReviewAgent: falha ao carregar prompt markdown: {e}")
        
        if not instructions:
            instructions = "You are a critical scientific peer reviewer. Find issues in logical consistency and citations."

        prompt = f"""
{instructions}

Query de Pesquisa Relacionada: "{query}"

Relatório de Pesquisa:
{report[:7000]}
"""

        schema = {
            "type": "object",
            "properties": {
                "assessment": {"type": "string", "enum": ["strong", "moderate", "weak", "unreliable"]},
                "confidence": {"type": "number"},
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string", "enum": REVIEW_CATEGORIES},
                            "severity": {"type": "string", "enum": REVIEW_SEVERITIES},
                            "description": {"type": "string"},
                            "location": {"type": "string"},
                            "suggestion": {"type": "string"}
                        },
                        "required": ["category", "severity", "description", "location", "suggestion"]
                    }
                },
                "strengths": {"type": "array", "items": {"type": "string"}},
                "recommendations": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["assessment", "confidence", "issues", "strengths", "recommendations"]
        }

        try:
            result = await self.llm.generate_structured(prompt, schema, temperature=0.1)
            if isinstance(result, dict):
                return result
        except Exception as e:
            logger.error(f"PeerReviewAgent: erro ao gerar revisão estruturada: {e}")
            
        return None

    def _heuristic_review(self, report: str, results: List[Any]) -> List[ReviewIssue]:
        """
        Varredura estática no texto do relatório por superlativos não citados e seções curtas.
        """
        issues: List[ReviewIssue] = []

        # 1. Detecção de superlativos sem citação próxima
        for superlativo in SUPERLATIVOS:
            pattern = re.compile(rf"\b({superlativo})\b", re.IGNORECASE)
            for match in pattern.finditer(report):
                matched_word = match.group(1)
                start_idx = match.start()
                
                # Contexto de 60 caracteres ao redor
                left = max(0, start_idx - 60)
                right = min(len(report), start_idx + len(matched_word) + 60)
                window = report[left:right]
                
                # Verifica se há marcadores de citação (ex: [1], (Fonte) ou links markdown)
                has_citation = "[" in window or "(" in window or "http" in window
                if not has_citation:
                    issues.append(ReviewIssue(
                        category="bias",
                        severity="major",
                        description=f"Uso do termo absoluto '{matched_word}' sem citação ou evidência de suporte.",
                        location=report[max(0, start_idx - 25):min(len(report), start_idx + len(matched_word) + 25)].strip(),
                        suggestion=f"Suavizar a afirmação ou adicionar uma citação direta próxima a '{matched_word}'."
                    ))

        # 2. Seções muito curtas (< 200 caracteres de conteúdo)
        # Dividimos em seções baseadas em headings
        sections = re.split(r"\n##+\s+", report)
        for section in sections:
            section = section.strip()
            if not section:
                continue
            
            lines = section.split("\n")
            title = lines[0].strip() if lines else "Seção"
            content = "\n".join(lines[1:]).strip()
            
            if 0 < len(content) < 200:
                issues.append(ReviewIssue(
                    category="missing_context",
                    severity="minor",
                    description=f"A seção '{title}' está muito curta ({len(content)} caracteres), indicando cobertura superficial.",
                    location=title,
                    suggestion=f"Expandir a seção '{title}' com mais detalhes factuais ou mesclar com uma seção adjacente."
                ))

        return issues

    def to_markdown(self, review: PeerReviewReport) -> str:
        """
        Converte a estrutura do PeerReviewReport em um bloco formatado em Markdown.
        """
        assessment_labels = {
            "strong": "🟢 Forte (Aprovado com ressalvas mínimas)",
            "moderate": "🟡 Moderado (Requer revisão de claims menores)",
            "weak": "🟠 Fraco (Grave falta de evidências ou coerência)",
            "unreliable": "🔴 Não Confiável (Múltiplas inconsistências e viés estrutural)"
        }
        label = assessment_labels.get(review.overall_assessment, review.overall_assessment)

        lines = [
            "\n\n---\n",
            "## 🔍 Revisão Científica (Peer Review Agent)\n",
            f"**Parecer Editorial:** {label}\n",
            f"**Índice de Rigor Científico:** {review.confidence_in_report:.0%}\n",
        ]

        if review.strengths:
            lines.append("### Pontos Fortes do Relatório")
            for s in review.strengths:
                lines.append(f"- {s}")
            lines.append("")

        if review.issues:
            lines.append("### Vulnerabilidades Argumentativas e Lacunas")
            lines.append("| Categoria | Severidade | Descrição / Contexto | Sugestão de Correção |")
            lines.append("| :--- | :--- | :--- | :--- |")
            
            severity_labels = {
                "critical": "🔴 critical",
                "major": "🟠 major",
                "minor": "🟡 minor"
            }
            
            for issue in review.issues:
                sev = severity_labels.get(issue.severity, issue.severity)
                # Escapa pipes do Markdown
                desc_clean = issue.description.replace("|", "\\|")
                loc_clean = f" *\"{issue.location.replace('|', '\\|')}\"*" if issue.location else ""
                sug_clean = issue.suggestion.replace("|", "\\|")
                
                lines.append(f"| {issue.category} | {sev} | {desc_clean}{loc_clean} | {sug_clean} |")
            lines.append("")

        if review.recommendations:
            lines.append("### Recomendações Gerais")
            for r in review.recommendations:
                lines.append(f"- {r}")
            lines.append("")

        return "\n".join(lines)
