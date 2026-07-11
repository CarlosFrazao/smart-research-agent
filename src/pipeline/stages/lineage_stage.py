"""Lineage Stage — Detecção de linhagem de citação dentro de cada cluster.

FASE 3 do Plano Parte 4 (Linhagem & Adversarial).

Responsabilidade
-----------------
Roda APÓS o ``cluster_similar_results()`` (em ``RankStage``) e ANTES do
``SynthesizeStage``. Para cada cluster com mais de 1 membro, classifica a
proveniência dos resultados:

  - ``primary``: o membro mais antigo (published_at) — candidato a origem.
  - ``derivative``: os demais, tipicamente reproduções/citações do primário.
  - ``cites_within_cluster``: IDs dos resultados cujo texto contém o domínio
    do primário, evidenciando citação direta.

Zero chamadas LLM na via rápida — só heurística de URL + data. Isso expõe a
diferença entre "10 fontes independentes" e "1 fonte primária + 9 derivadas",
alimentando a seção de confiança do relatório.

Design
------
Recebe o ``PipelineContext`` e muta ``ranked_results`` in-place (preenche
``lineage_role`` / ``cites_within_cluster``). Tolerante a resultados sem
``cluster_id`` (não clusterizados) e sem ``published_at`` (ordena por
``datetime.max`` como sentinela de "mais novo possível").
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from src.pipeline.pipeline import PipelineContext, PipelineStage

logger = logging.getLogger("pipeline.lineage_stage")


class LineageStage(PipelineStage):
    """Detecta linhagem de citação dentro de cada cluster de resultados.

    Attributes:
        name: Identificador do stage no pipeline.
        critical: False — é um enriquecimento não-fatal; se falhar, o pipeline
            prossegue sem a linhagem (não deve abortar a pesquisa).
    """

    name = "lineage"
    critical = False

    async def run(self, context: PipelineContext) -> PipelineContext:
        """Classifica a linhagem de citação de cada cluster em ``ranked_results``.

        Args:
            context: Contexto do pipeline (lê/muta ``ranked_results``).

        Returns:
            PipelineContext: O mesmo contexto, com ``lineage_role`` e
            ``cites_within_cluster`` preenchidos nos membros de clusters.
        """
        results = context.ranked_results or []
        if not results:
            logger.debug("LineageStage: sem resultados ranqueados; nada a fazer.")
            return context

        clusters = self._group_by_cluster(results)
        if not clusters:
            logger.debug("LineageStage: nenhum cluster com >1 membro; nada a fazer.")
            return context

        total_primary = 0
        total_derivative = 0
        for cluster_id, members in clusters.items():
            classified = self._classify_lineage(members)
            total_primary += classified["primary"]
            total_derivative += classified["derivative"]
            logger.info(
                "LineageStage: cluster '%s' classificado (%d primário, %d derivado).",
                cluster_id,
                classified["primary"],
                classified["derivative"],
            )

        context.set(
            "lineage_summary",
            {
                "clusters": len(clusters),
                "primary": total_primary,
                "derivative": total_derivative,
            },
        )
        return context

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _group_by_cluster(results: list[Any]) -> dict[str, list[Any]]:
        """Agrupa resultados por ``cluster_id`` (só clusters com >1 membro)."""
        clusters: dict[str, list[Any]] = {}
        for r in results:
            cid = getattr(r, "cluster_id", None)
            if cid:
                clusters.setdefault(cid, []).append(r)
        # Descarta clusters de membro único — linhagem só faz sentido em grupo.
        return {cid: members for cid, members in clusters.items() if len(members) > 1}

    @staticmethod
    def _extract_domain(url: str | None) -> str | None:
        """Extrai o domínio de uma URL (sem 'www.'), ou None se inválida."""
        if not url:
            return None
        try:
            host = urlparse(url).netloc
            return host[4:] if host.startswith("www.") else host or None
        except Exception as exc:  # noqa: BLE001 - defesa contra URLs malformadas
            logger.debug("LineageStage: falha ao extrair domínio de '%s': %s", url, exc)
            return None

    def _classify_lineage(self, members: list[Any]) -> dict[str, int]:
        """Classifica a linhagem de um cluster (ordena por published_at).

        O membro mais antigo vira ``primary``. Os demais são ``derivative``;
        se o texto de um derivado contiver o domínio do primário, registra-se
        o ``result_id`` do primário em ``cites_within_cluster`` como evidência
        de citação direta.

        Returns:
            dict com contagem de ``primary`` e ``derivative``.
        """
        # 1. Ordenar por published_at (mais antigo primeiro; None -> "mais novo").
        sorted_members = sorted(
            members,
            key=lambda r: (getattr(r, "published_at", None) or datetime.max),
        )

        primary = sorted_members[0]
        primary_domain = self._extract_domain(getattr(primary, "url", None))
        self._safe_set(primary, "lineage_role", "primary")

        counts = {"primary": 1, "derivative": 0}

        # 2. Verificar citação nos demais (regex de domínio no texto).
        for member in sorted_members[1:]:
            member_role = "derivative"
            if primary_domain:
                text = f"{getattr(member, 'description', '') or ''} {getattr(member, 'raw', '') or ''}"
                if primary_domain in text:
                    primary_id = getattr(primary, "result_id", None)
                    if primary_id:
                        cites = getattr(member, "cites_within_cluster", None)
                        if cites is not None and primary_id not in cites:
                            cites.append(primary_id)
            self._safe_set(member, "lineage_role", member_role)
            counts[member_role] += 1

        return counts

    @staticmethod
    def _safe_set(obj: Any, attr: str, value: Any) -> None:
        """Atribui ``value`` a ``obj.attr`` sem quebrar se o modelo rejeitar."""
        try:
            setattr(obj, attr, value)
        except Exception as exc:  # noqa: BLE001 - modelo pode ser read-only em teste
            logger.debug(
                "LineageStage: não foi possível setar %s=%r em %r: %s",
                attr,
                value,
                obj,
                exc,
            )


__all__ = ["LineageStage"]
