"""
temporal_analyzer.py — Análise Temporal e Tendências (Bloco 4.1)

Extrai informações de data dos resultados da pesquisa, constrói uma linha do tempo (timeline),
computa um histograma de interesse e detecta a tendência geral usando regressão linear simples.
"""
import re
import logging
from datetime import datetime
from typing import Any, Dict, List, Tuple, Optional

logger = logging.getLogger("temporal_analyzer")

# Padrões regex para encontrar datas no texto (descrições, títulos, destaques, etc.)
DATE_PATTERNS = [
    r"\b(\d{4})-(\d{2})-(\d{2})\b",  # YYYY-MM-DD
    r"\b(\d{2})/(\d{4})\b",           # MM/YYYY
    r"\b(janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+de\s+(\d{4})\b", # Mês de YYYY
    r"\b(19\d{2}|20\d{2})\b",         # YYYY isolado (entre 1900 e 2099)
]

MONTHS_MAP = {
    "janeiro": 1, "jan": 1, "fevereiro": 2, "feb": 2, "março": 3, "mar": 3,
    "abril": 4, "apr": 4, "maio": 5, "may": 5, "junho": 6, "jun": 6,
    "julho": 7, "jul": 7, "agosto": 8, "aug": 8, "setembro": 9, "sep": 9,
    "outubro": 10, "oct": 10, "novembro": 11, "nov": 11, "dezembro": 12, "dec": 12
}


class TemporalAnalyzer:
    """
    Analisa os resultados sob a perspectiva temporal.
    Extrai datas, calcula tendências de relevância/volume no tempo e formata a timeline.
    """

    def __init__(self):
        pass

    def extract_timeline(self, results: List[Any]) -> List[Tuple[datetime, str, str]]:
        """
        Extrai referências temporais dos resultados.
        Retorna uma lista de tuplas (data, titulo_projeto, contexto_ou_descricao) ordenadas por data.
        """
        timeline: List[Tuple[datetime, str, str]] = []

        for r in results:
            title = getattr(r, "title", "(sem título)")
            
            # Coleta textos associados a esse resultado para buscar datas
            texts_to_search = []
            if getattr(r, "description", None):
                texts_to_search.append(r.description)
            if getattr(r, "highlights", None):
                texts_to_search.extend(r.highlights)

            # Também considera metadados explícitos de data se existirem
            metrics = getattr(r, "metrics", {}) or {}
            updated_at = metrics.get("updated_at")
            if updated_at:
                if isinstance(updated_at, datetime):
                    timeline.append((updated_at, title, "Última atualização do repositório/fonte"))
                elif isinstance(updated_at, str):
                    try:
                        # Limpa string para tentar parsear
                        clean_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                        timeline.append((clean_dt, title, "Última atualização do repositório/fonte"))
                    except ValueError:
                        pass

            # Busca datas por regex nos textos livres
            for text in texts_to_search:
                if not text:
                    continue
                
                matched_intervals = []

                # 1. YYYY-MM-DD
                for m in re.finditer(DATE_PATTERNS[0], text):
                    try:
                        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                        timeline.append((dt, title, text[max(0, m.start()-40):min(len(text), m.end()+40)].strip()))
                        matched_intervals.append((m.start(), m.end()))
                    except ValueError:
                        pass

                # 2. MM/YYYY
                for m in re.finditer(DATE_PATTERNS[1], text):
                    try:
                        dt = datetime(int(m.group(2)), int(m.group(1)), 1)
                        timeline.append((dt, title, text[max(0, m.start()-40):min(len(text), m.end()+40)].strip()))
                        matched_intervals.append((m.start(), m.end()))
                    except ValueError:
                        pass

                # 3. Mês de YYYY
                for m in re.finditer(DATE_PATTERNS[2], text, re.IGNORECASE):
                    try:
                        month_name = m.group(1).lower()
                        month = MONTHS_MAP.get(month_name, 1)
                        year = int(m.group(2))
                        dt = datetime(year, month, 1)
                        timeline.append((dt, title, text[max(0, m.start()-40):min(len(text), m.end()+40)].strip()))
                        matched_intervals.append((m.start(), m.end()))
                    except ValueError:
                        pass

                # 4. YYYY isolado (evitando capturar partes de números de versões ou IDs maiores)
                # Só processa se não pegou nada mais específico no mesmo segmento
                for m in re.finditer(DATE_PATTERNS[3], text):
                    start_idx = m.start()
                    end_idx = m.end()

                    # Evita casamento duplicado se o ano está dentro de uma data já casada (ex: 2023 em 2023-05-15)
                    overlap = False
                    for s, e in matched_intervals:
                        if s <= start_idx < e:
                            overlap = True
                            break
                    if overlap:
                        continue

                    year_val = int(m.group(1))

                    # Garante que não é parte de uma versão (como "v1.2024" ou "2024.1")
                    if start_idx > 0 and text[start_idx-1] in (".", "v", "V"):
                        continue
                    if end_idx < len(text) and text[end_idx] == ".":
                        # Só descarta se o caractere depois do ponto for dígito (ex: 2026.1)
                        if end_idx + 1 < len(text) and text[end_idx+1].isdigit():
                            continue

                    dt = datetime(year_val, 1, 1)
                    timeline.append((dt, title, text[max(0, m.start()-30):min(len(text), m.end()+30)].strip()))

        # Remove duplicados exatos (mesma data, projeto e descrição próxima)
        seen = set()
        unique_timeline = []
        for dt, proj, desc in timeline:
            # Normaliza descrição para evitar ruídos de offset do regex
            desc_norm = re.sub(r"\s+", " ", desc)[:30]
            key = (dt.date(), proj, desc_norm)
            if key not in seen:
                seen.add(key)
                unique_timeline.append((dt, proj, desc))

        # Ordena cronologicamente
        unique_timeline.sort(key=lambda x: x[0])
        return unique_timeline

    def compute_histogram(self, timeline: List[Tuple[datetime, str, str]]) -> Dict[str, int]:
        """
        Agrupa os eventos em um histograma por ano (ou YYYY-MM se o intervalo for muito curto).
        Retorna dicionário { "YYYY": contagem } ordenado por ano.
        """
        histogram: Dict[str, int] = {}
        for dt, _, _ in timeline:
            key = str(dt.year)
            histogram[key] = histogram.get(key, 0) + 1
        
        # Ordena por ano string
        return dict(sorted(histogram.items()))

    def detect_trend(self, histogram: Dict[str, int]) -> str:
        """
        Mapeia a tendência temporal com base em regressão linear simples (mínimos quadrados ordinários).
        Retorna "crescente" | "decrescente" | "estável" | "dados insuficientes".
        """
        if len(histogram) < 2:
            return "dados insuficientes"

        # Ordena anos para ter uma sequência temporal
        sorted_years = sorted(histogram.keys(), key=int)
        x = list(range(len(sorted_years)))
        y = [histogram[yr] for yr in sorted_years]

        n = len(x)
        mean_x = sum(x) / n
        mean_y = sum(y) / n

        numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
        denominator = sum((x[i] - mean_x) ** 2 for i in range(n))

        if denominator == 0:
            slope = 0.0
        else:
            slope = numerator / denominator

        # Normaliza a inclinação em relação à média dos dados para definir o limiar
        if mean_y > 0:
            normalized_slope = slope / mean_y
        else:
            normalized_slope = slope

        logger.debug(f"Trend detection: slope={slope:.4f}, normalized={normalized_slope:.4f}")

        # Limiares de classificação
        if normalized_slope > 0.08:
            return "crescente"
        elif normalized_slope < -0.08:
            return "decrescente"
        else:
            return "estável"

    def generate_timeline_section(self, results: List[Any]) -> str:
        """
        Gera uma seção Markdown pronta para ser injetada no relatório.
        """
        timeline = self.extract_timeline(results)
        if not timeline:
            return "### 📅 Linha do Tempo & Tendências\n\nNenhuma informação temporal significativa pôde ser extraída dos dados coletados.\n"

        histogram = self.compute_histogram(timeline)
        trend = self.detect_trend(histogram)
        
        trend_badges = {
            "crescente": "📈 **Tendência Crescente (Alta de Interesse / Atividade)**",
            "decrescente": "📉 **Tendência Decrescente (Queda de Interesse / Atividade)**",
            "estável": "➡️ **Tendência Estável / Consolidada**",
            "dados insuficientes": "❓ **Dados Temporais Insuficientes para Análise de Tendência**",
        }
        trend_display = trend_badges.get(trend, trend)

        lines = [
            "## 📅 Linha do Tempo & Análise Temporal",
            "",
            f"**Análise de Tendência:** {trend_display}",
            "",
            "### 📊 Histograma de Menções/Atividade por Ano",
            "",
            "| Ano | Ocorrências / Eventos | Histórico Visual |",
            "|-----|------------------------|------------------|",
        ]

        # Gera barrinhas visuais em Markdown simples
        max_count = max(histogram.values()) if histogram else 1
        for yr, count in histogram.items():
            bar_len = int((count / max_count) * 10) if max_count > 0 else 0
            bar = "█" * max(1, bar_len)
            lines.append(f"| {yr} | {count} | `{bar}` |")

        lines += [
            "",
            "### 🗓️ Linha do Tempo Cronológica",
            "",
        ]

        # Limita a timeline visível a no máximo 10 marcos mais importantes
        # para evitar poluir o relatório.
        for dt, proj, desc in timeline[:10]:
            clean_desc = desc.replace("\n", " ").strip()
            # Destaca datas
            date_str = dt.strftime("%d/%m/%Y") if dt.day != 1 or dt.month != 1 else str(dt.year)
            lines.append(f"- **{date_str}** — *{proj}*: ... {clean_desc} ...")

        if len(timeline) > 10:
            lines.append(f"\n*(Mais {len(timeline) - 10} referências temporais ocultadas para concisão)*")

        lines.append("")
        return "\n".join(lines)
