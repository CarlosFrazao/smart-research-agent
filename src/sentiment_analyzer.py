"""
sentiment_analyzer.py — Análise de Sentimentos e Viés (Bloco 4.2)

Analisa o sentimento de cada resultado de pesquisa e do relatório sintetizado geral,
detectando vieses emocionais ou falta de neutralidade técnica.
Suporta vaderSentiment (se instalado) com fallback baseado em léxico estático.
"""
import re
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sentiment_analyzer")

# Léxico simples para fallback de sentimento (Português e Inglês)
POSITIVE_LEXICON = {
    "excelente", "ótimo", "bom", "incrível", "rápido", "fácil", "moderno", "seguro", "inovador", "sucesso", "eficiente",
    "great", "excellent", "good", "amazing", "fast", "easy", "modern", "secure", "innovative", "love", "like", "best",
    "awesome", "perfect", "clean", "robust", "powerful", "popular", "stable", "valioso", "recomendo", "recomenda", "top"
}

NEGATIVE_LEXICON = {
    "ruim", "lento", "difícil", "ruído", "inseguro", "complicado", "antigo", "erro", "falha", "defeito", "bug", "problema",
    "bad", "slow", "hard", "difficult", "insecure", "complicated", "old", "error", "fail", "defect", "worst", "hate",
    "issue", "broke", "expensive", "caro", "limitado", "limitação", "warn", "warning", "flaw", "incompatível"
}

# Mapeamento de nomes de fontes para exibição human-readable
SOURCE_DISPLAY_MAP = {
    "github": "GitHub",
    "reddit": "Reddit",
    "hackernews": "Hacker News",
    "arxiv": "ArXiv",
    "pubmed": "PubMed",
    "youtube": "YouTube",
    "semantic_scholar": "Semantic Scholar",
    "producthunt": "Product Hunt",
    "stackoverflow": "Stack Overflow",
}


class SentimentAnalyzer:
    """
    Analisa os sentimentos (positivo, negativo, neutro, polaridade) nas fontes e relatórios.
    Identifica se a cobertura de um tópico está excessivamente parcial (viés).
    """

    def __init__(self):
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self.vader = SentimentIntensityAnalyzer()
            self.has_vader = True
            logger.debug("SentimentAnalyzer: VADER carregado com sucesso.")
        except ImportError:
            self.vader = None
            self.has_vader = False
            logger.debug("SentimentAnalyzer: VADER não instalado — usando analisador léxico de fallback.")

    def _fallback_score(self, text: str) -> Dict[str, float]:
        """Análise de sentimento simplificada baseada em léxico de fallback."""
        if not text:
            return {"pos": 0.0, "neg": 0.0, "neu": 1.0, "compound": 0.0}

        # Tokenização simples
        words = re.findall(r"\b\w+\b", text.lower())
        if not words:
            return {"pos": 0.0, "neg": 0.0, "neu": 1.0, "compound": 0.0}

        pos_count = sum(1 for w in words if w in POSITIVE_LEXICON)
        neg_count = sum(1 for w in words if w in NEGATIVE_LEXICON)

        total_emotional = pos_count + neg_count
        if total_emotional == 0:
            return {"pos": 0.0, "neg": 0.0, "neu": 1.0, "compound": 0.0}

        # Frações
        total_words = len(words)
        pos_frac = pos_count / total_words
        neg_frac = neg_count / total_words
        neu_frac = (total_words - total_emotional) / total_words

        # Compound simples normalizado entre -1.0 e 1.0
        diff = pos_count - neg_count
        compound = diff / total_emotional

        return {
            "pos": round(pos_frac, 3),
            "neg": round(neg_frac, 3),
            "neu": round(neu_frac, 3),
            "compound": round(compound, 3)
        }

    def score_result(self, result: Any) -> Dict[str, float]:
        """
        Calcula o sentimento de um SearchResult com base no título e descrição.
        Retorna dicionário com polaridades (pos, neg, neu, compound).
        """
        title = getattr(result, "title", "") or ""
        desc = getattr(result, "description", "") or ""
        text = f"{title} {desc}".strip()

        if self.has_vader:
            try:
                scores = self.vader.polarity_scores(text)
                return {
                    "pos": scores["pos"],
                    "neg": scores["neg"],
                    "neu": scores["neu"],
                    "compound": scores["compound"]
                }
            except Exception as e:
                logger.warning(f"SentimentAnalyzer: Erro no VADER: {e}")
                return self._fallback_score(text)
        else:
            return self._fallback_score(text)

    def score_neutrality(self, report: str) -> float:
        """
        Avalia o quão neutro/técnico é o texto do relatório.
        Retorna um valor entre 0.0 (parcial/emocional) e 1.0 (totalmente neutro).
        """
        if not report:
            return 1.0

        if self.has_vader:
            try:
                # O VADER é otimizado para frases curtas, então pegamos uma amostra
                # ou calculamos a polaridade média dos parágrafos principais.
                paragraphs = [p.strip() for p in report.split("\n\n") if len(p.strip()) > 30]
                if not paragraphs:
                    scores = self.vader.polarity_scores(report[:2000])
                    return round(1.0 - abs(scores["compound"]), 3)
                
                compounds = []
                for p in paragraphs[:15]:  # Amostra das primeiras seções
                    compounds.append(self.vader.polarity_scores(p)["compound"])
                
                mean_compound = sum(compounds) / len(compounds)
                return round(1.0 - abs(mean_compound), 3)
            except Exception as e:
                logger.warning(f"SentimentAnalyzer: Erro no VADER ao medir neutralidade: {e}")
        
        # Fallback
        scores = self._fallback_score(report)
        return round(1.0 - abs(scores["compound"]), 3)

    def check_bias(self, results: List[Any]) -> Optional[str]:
        """
        Analisa o viés coletivo dos resultados.
        Se a média de compound for muito positiva ou negativa, aponta um viés.
        """
        if not results:
            return None

        compounds = []
        for r in results:
            scores = self.score_result(r)
            compounds.append(scores["compound"])

        mean_compound = sum(compounds) / len(compounds)

        if mean_compound > 0.35:
            return (
                "⚠️ **Viés Positivo (Entusiasmo Coletivo):** As fontes analisadas apresentam "
                "um tom predominantemente otimista ou promocional. Recomenda-se cautela, "
                "pois limitações ou desvantagens técnicas podem estar sub-representadas nas referências."
            )
        elif mean_compound < -0.35:
            return (
                "⚠️ **Viés Negativo (Crítica Concentrada):** As fontes apresentam um tom "
                "predominantemente insatisfeito, crítico ou focado em falhas. Verifique se as críticas "
                "são pontuais de fóruns/comunidades ou se refletem o estado geral da ferramenta."
            )
        else:
            return None

    def generate_sentiment_section(self, results: List[Any]) -> str:
        """
        Gera a seção Markdown formatada pronta para injeção no relatório.
        """
        if not results:
            return "## 🎭 Análise de Sentimento\n\nNenhum dado disponível para análise de sentimento.\n"

        # Coleta sentimentos individuais das fontes
        sources_sentiment: Dict[str, List[float]] = {}
        for r in results:
            source = getattr(r, "source", None)
            if not source and getattr(r, "sources", None):
                source = r.sources[0]
            if not source:
                source = "unknown"
            
            scores = self.score_result(r)
            sources_sentiment.setdefault(source, []).append(scores["compound"])

        # Calcula a média por fonte
        lines = [
            "## 🎭 Análise de Sentimento & Viés",
            "",
            "Esta seção analisa a recepção e o tom das discussões sobre as tecnologias identificadas nas fontes originais.",
            "",
            "### 📊 Perfil de Sentimento por Canal de Origem",
            "",
            "| Canal de Origem | Relevância / Volume | Tom Médio / Sentimento | Classificação |",
            "|-----------------|---------------------|------------------------|---------------|",
        ]

        total_compounds = []
        for src, comps in sorted(sources_sentiment.items()):
            mean_c = sum(comps) / len(comps)
            total_compounds.extend(comps)
            
            # Formata emoji de tom
            if mean_c > 0.15:
                tone = "🟢 Positivo"
            elif mean_c < -0.15:
                tone = "🔴 Negativo"
            else:
                tone = "🟡 Neutro / Técnico"
            
            src_display = SOURCE_DISPLAY_MAP.get(src.lower(), src.capitalize())
            lines.append(f"| {src_display} | {len(comps)} item(ns) | `{mean_c:+.2f}` | {tone} |")

        lines.append("")

        # Avalia viés geral dos resultados
        bias_warning = self.check_bias(results)
        if bias_warning:
            lines += [
                "### ⚖️ Detecção de Viés / Parcialidade",
                "",
                bias_warning,
                "",
            ]
        else:
            lines += [
                "### ⚖️ Detecção de Viés / Parcialidade",
                "",
                "✅ **Equilíbrio Editorial:** As referências coletadas mesclam discussões de tom neutro, técnico "
                "e debates saudáveis, sem indícios de viés sistemático (entusiasmo publicitário ou rejeição cega).",
                "",
            ]

        # Calcula neutralidade teórica
        overall_neutrality = 1.0 - abs(sum(total_compounds) / len(total_compounds)) if total_compounds else 1.0
        neutrality_pct = overall_neutrality * 100
        
        # Barra visual de neutralidade
        bar_len = int((overall_neutrality) * 10)
        bar = "█" * max(1, bar_len) + "░" * (10 - max(1, bar_len))

        lines += [
            "### ⚙️ Índice de Neutralidade Técnica",
            "",
            f"**Score de Objetividade:** `{neutrality_pct:.1f}%`",
            f"`[{bar}]`",
            "",
            "*(Um índice acima de 70% indica que as referências são predominantemente descritivas e técnicas,"
            " focando em dados empíricos em vez de opiniões polarizadas).* ",
            ""
        ]

        return "\n".join(lines)
