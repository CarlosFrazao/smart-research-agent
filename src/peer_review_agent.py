"""Agente de revisao de pares que valida a qualidade e coerencia dos resultados antes de gerar o relatorio."""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from src.clients.llm_client import LLMClient

logger = logging.getLogger("peer_review_agent")

REVIEW_CATEGORIES = [
    "logical_fallacy",  # Falhas de coerência, saltos lógicos
    "unsupported_claim",  # Afirmações sem evidência citada
    "cherry_picking",  # Evidências contrárias ignoradas
    "bias",  # Viés e tendenciosidade
    "missing_context",  # Limitações não mencionadas
    "weak_citation",  # URLs suspeitos, fontes secundárias
]
REVIEW_SEVERITIES = ["critical", "major", "minor"]
SUPERLATIVOS = [
    "melhor",
    "único",
    "definitivo",
    "100%",
    "sempre",
    "nunca",
    "revolucionário",
    "perfeito",
    "impossível",
]

# Padrão para reconhecer uma citação de verdade perto de um superlativo:
# link markdown "[texto](url)", referência numérica "[1]", uma URL nua, ou
# a palavra "fonte". Antes disso, o código considerava QUALQUER parêntese
# ou colchete como "citado" — o que faz um parêntese comum qualquer
# (ex.: "sempre (segundo alguns) o melhor") mascarar um superlativo sem
# nenhuma evidência de fato, esvaziando boa parte da checagem de viés.
_CITATION_PATTERN = re.compile(
    r"\[[^\]]*\]\([^)]+\)|\[\d+\]|https?://|\bfonte\b", re.IGNORECASE
)


@dataclass
class ReviewIssue:
    category: str
    severity: str
    description: str
    location: str
    suggestion: str


@dataclass
class PeerReviewReport:
    overall_assessment: str  # strong | moderate | weak | unreliable
    confidence_in_report: float  # 0.0-1.0
    issues: list[ReviewIssue] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "critical")

    @property
    def major_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "major")

    @property
    def minor_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "minor")


def _clamp_confidence(value: Any, default: float = 0.70) -> float:
    """Converte `value` em float dentro de 0.0-1.0.

    Protege `to_markdown` (que formata com `:.0%`) contra um LLM devolvendo
    a confiança como porcentagem inteira (ex.: 95 em vez de 0.95), como
    string, ou fora de faixa — qualquer um desses casos, sem esse guard,
    gera saída absurda (ex.: "9500%") ou levanta exceção na formatação.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v > 1.0:
        v = v / 100.0  # provavelmente veio como porcentagem (0-100)
    return max(0.0, min(1.0, v))


class PeerReviewAgent:
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
        self.prompt_path = os.path.join("prompts", "peer_review.md")

    async def review(
        self, report: str, results: list[Any], query: str = ""
    ) -> PeerReviewReport:
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
                recommendations=["Realizar revisão manual das fontes."],
            )

        # Mescla as issues das duas fontes
        all_issues = list(heuristic_issues)
        seen_descriptions = {i.description.lower() for i in all_issues}

        for issue_dict in structured_report.get("issues") or []:
            desc = issue_dict.get("description", "")
            if desc.lower() not in seen_descriptions:
                all_issues.append(
                    ReviewIssue(
                        category=issue_dict.get("category", "unsupported_claim"),
                        severity=issue_dict.get("severity", "minor"),
                        description=desc,
                        location=issue_dict.get("location", ""),
                        suggestion=issue_dict.get("suggestion", ""),
                    )
                )
                seen_descriptions.add(desc.lower())

        return PeerReviewReport(
            # O schema enviado ao LLM define a chave "assessment"/"confidence"
            # (não "overall_assessment"/"confidence_in_report"); mantemos o
            # fallback duplo apenas por robustez a variações do modelo, mas
            # a chave real do schema vem primeiro para não sugerir que
            # "overall_assessment" é o campo esperado.
            overall_assessment=str(
                structured_report.get("assessment")
                or structured_report.get("overall_assessment")
                or "moderate"
            ),
            confidence_in_report=_clamp_confidence(
                structured_report.get(
                    "confidence", structured_report.get("confidence_in_report", 0.70)
                )
            ),
            issues=all_issues,
            strengths=structured_report.get("strengths") or [],
            recommendations=structured_report.get("recommendations") or [],
        )

    async def _structured_review(
        self, report: str, query: str
    ) -> dict[str, Any] | None:
        """
        Carrega as regras do arquivo markdown e envia o prompt estruturado ao LLM.
        """
        instructions = ""
        if os.path.exists(self.prompt_path):
            try:
                with open(self.prompt_path, encoding="utf-8") as f:
                    instructions = f.read()
            except Exception as e:
                logger.warning(
                    f"PeerReviewAgent: falha ao carregar prompt markdown: {e}"
                )

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
                "assessment": {
                    "type": "string",
                    "enum": ["strong", "moderate", "weak", "unreliable"],
                },
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
                            "suggestion": {"type": "string"},
                        },
                        "required": [
                            "category",
                            "severity",
                            "description",
                            "location",
                            "suggestion",
                        ],
                    },
                },
                "strengths": {"type": "array", "items": {"type": "string"}},
                "recommendations": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "assessment",
                "confidence",
                "issues",
                "strengths",
                "recommendations",
            ],
        }

        try:
            result = await self.llm.generate_structured(prompt, schema, temperature=0.1)
            if isinstance(result, dict):
                return result
        except Exception as e:
            logger.error(f"PeerReviewAgent: erro ao gerar revisão estruturada: {e}")

        return None

    def _heuristic_review(self, report: str, results: list[Any]) -> list[ReviewIssue]:
        """
        Varredura estática no texto do relatório por superlativos não citados,
        seções curtas, e citações que apontam para fora do conjunto de fontes
        coletadas.
        """
        issues: list[ReviewIssue] = []

        # 1. Detecção de superlativos sem citação próxima
        for superlativo in SUPERLATIVOS:
            # Usa lookaround (?<!\w)...(?!\w) em vez de \b nas duas pontas.
            # \b exige uma transição \w<->\W; para termos que TERMINAM em
            # caractere não-alfanumérico (como "100%"), o \b final só
            # existiria se o próximo caractere do texto fosse letra/dígito
            # — o que quase nunca acontece (normalmente vem espaço/pontuação)
            # — então "100%" NUNCA era detectado com \b. O lookaround
            # funciona igual ao \b para os demais termos e corrige esse caso.
            pattern = re.compile(
                rf"(?<!\w)({re.escape(superlativo)})(?!\w)", re.IGNORECASE
            )
            for match in pattern.finditer(report):
                matched_word = match.group(1)
                start_idx = match.start()

                # Contexto de 60 caracteres ao redor
                left = max(0, start_idx - 60)
                right = min(len(report), start_idx + len(matched_word) + 60)
                window = report[left:right]

                # Verifica se há uma citação de verdade (link markdown,
                # referência numérica, URL nua ou a palavra "fonte") —
                # não apenas qualquer parêntese/colchete no texto.
                has_citation = bool(_CITATION_PATTERN.search(window))
                if not has_citation:
                    issues.append(
                        ReviewIssue(
                            category="bias",
                            severity="major",
                            description=f"Uso do termo absoluto '{matched_word}' sem citação ou evidência de suporte.",
                            location=report[
                                max(0, start_idx - 25) : min(
                                    len(report), start_idx + len(matched_word) + 25
                                )
                            ].strip(),
                            suggestion=f"Suavizar a afirmação ou adicionar uma citação direta próxima a '{matched_word}'.",
                        )
                    )

        # 2. Seções muito curtas (< 200 caracteres de conteúdo)
        # Dividimos em seções baseadas em headings
        sections = re.split(r"\n##+\s+", report)
        # O primeiro elemento do split é o texto ANTES do primeiro heading
        # "##"; se o relatório não começa com um heading, esse trecho é só
        # um preâmbulo (ex.: título "# Relatório..." + intro), não uma
        # seção nomeada — tratá-lo como seção gera falso positivo do tipo
        # "a seção 'Este é um relatório sobre...' está muito curta".
        report_starts_with_heading = bool(re.match(r"^#+\s", report.lstrip()))

        for idx, section in enumerate(sections):
            section = section.strip()
            if not section:
                continue
            if idx == 0 and not report_starts_with_heading:
                continue  # preâmbulo antes do primeiro heading, não é uma seção

            lines = section.split("\n")
            title = lines[0].strip() if lines else "Seção"
            content = "\n".join(lines[1:]).strip()

            if 0 < len(content) < 200:
                issues.append(
                    ReviewIssue(
                        category="missing_context",
                        severity="minor",
                        description=f"A seção '{title}' está muito curta ({len(content)} caracteres), indicando cobertura superficial.",
                        location=title,
                        suggestion=f"Expandir a seção '{title}' com mais detalhes factuais ou mesclar com uma seção adjacente.",
                    )
                )

        # 3. Citações que apontam para URLs fora do conjunto de fontes
        # coletadas nesta pesquisa. `results` era recebido por este método
        # e nunca utilizado; esta checagem dá uso real ao parâmetro e cobre
        # a categoria "weak_citation" (já existia em REVIEW_CATEGORIES mas
        # nunca era produzida pela via heurística, só pelo LLM).
        known_urls = {url for r in (results or []) if (url := getattr(r, "url", None))}
        if known_urls:
            for match in re.finditer(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", report):
                link_text, url = match.group(1), match.group(2)
                if url not in known_urls:
                    issues.append(
                        ReviewIssue(
                            category="weak_citation",
                            severity="major",
                            description=(
                                "Citação aponta para uma URL que não está entre "
                                f"as fontes coletadas nesta pesquisa: {url}"
                            ),
                            location=link_text,
                            suggestion=(
                                "Confirmar se a URL é uma fonte válida coletada "
                                "pelo pipeline; se não for, remover ou corrigir "
                                "a citação."
                            ),
                        )
                    )

        return issues

    def to_markdown(self, review: PeerReviewReport) -> str:
        """
        Converte a estrutura do PeerReviewReport em um bloco formatado em Markdown.
        """
        assessment_labels = {
            "strong": "🟢 Forte (Aprovado com ressalvas mínimas)",
            "moderate": "🟡 Moderado (Requer revisão de claims menores)",
            "weak": "🟠 Fraco (Grave falta de evidências ou coerência)",
            "unreliable": "🔴 Não Confiável (Múltiplas inconsistências e viés estrutural)",
        }
        label = assessment_labels.get(
            review.overall_assessment, review.overall_assessment
        )

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
            lines.append(
                "| Categoria | Severidade | Descrição / Contexto | Sugestão de Correção |"
            )
            lines.append("| :--- | :--- | :--- | :--- |")

            severity_labels = {
                "critical": "🔴 critical",
                "major": "🟠 major",
                "minor": "🟡 minor",
            }

            for issue in review.issues:
                sev = severity_labels.get(issue.severity, issue.severity)
                # Escapa pipes e quebras de linha do Markdown
                desc_clean = (
                    issue.description.replace("|", "\\|")
                    .replace("\n", " ")
                    .replace("\r", " ")
                )
                loc_clean = ""
                if issue.location:
                    loc_escaped = (
                        issue.location.replace("|", "\\|")
                        .replace("\n", " ")
                        .replace("\r", " ")
                    )
                    loc_clean = f' *"{loc_escaped}"*'
                sug_clean = (
                    issue.suggestion.replace("|", "\\|")
                    .replace("\n", " ")
                    .replace("\r", " ")
                )

                lines.append(
                    f"| {issue.category} | {sev} | {desc_clean}{loc_clean} | {sug_clean} |"
                )
            lines.append("")

        if review.recommendations:
            lines.append("### Recomendações Gerais")
            for r in review.recommendations:
                lines.append(f"- {r}")
            lines.append("")

        return "\n".join(lines)
