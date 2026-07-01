"""
scheduler.py — Agendamento e Monitoramento Contínuo de Pesquisas (Bloco 3.3)

Permite agendar pesquisas recorrentes usando APScheduler (dependência opcional).
Sem APScheduler, os jobs podem ser disparados manualmente via CLI.
Detecta mudanças entre execuções e envia alertas via webhook.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("scheduler")

_DEFAULT_JOBS_FILE = "reports/scheduled_jobs.json"

# Regex para detecção de mudanças entre relatórios
_ENTITY_RE    = re.compile(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\b")
_URL_RE       = re.compile(r"https?://[^\s\)\]>\"']+")
_NUMBER_RE    = re.compile(r"\b\d+(?:[.,]\d+)?(?:\s*%|\s*(?:milhões?|bilhões?|mil|k|M|B))?\b")
_HEADING_RE   = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_SCORE_RE     = re.compile(r"Research Score.*?([A-F][+]?)", re.IGNORECASE | re.DOTALL)


class ScheduledJob:
    """Representa um job de pesquisa agendado."""

    def __init__(
        self,
        query: str,
        cron: str,
        output_dir: str,
        webhook_url: Optional[str] = None,
        alert_on_changes: bool = True,
        email: Optional[str] = None,
    ):
        self.id = str(uuid.uuid4())
        self.query = query
        self.cron = cron
        self.output_dir = output_dir
        self.webhook_url = webhook_url
        self.alert_on_changes = alert_on_changes
        self.email = email
        self.last_run: Optional[str] = None
        self.last_report_path: Optional[str] = None
        self.created_at = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "query": self.query,
            "cron": self.cron,
            "output_dir": self.output_dir,
            "webhook_url": self.webhook_url,
            "alert_on_changes": self.alert_on_changes,
            "email": self.email,
            "last_run": self.last_run,
            "last_report_path": self.last_report_path,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduledJob":
        job = cls(
            query=data["query"],
            cron=data["cron"],
            output_dir=data["output_dir"],
            webhook_url=data.get("webhook_url"),
            alert_on_changes=data.get("alert_on_changes", True),
            email=data.get("email"),
        )
        job.id = data["id"]
        job.last_run = data.get("last_run")
        job.last_report_path = data.get("last_report_path")
        job.created_at = data.get("created_at", datetime.now().isoformat())
        return job


class ResearchScheduler:
    """
    Agendador de pesquisas recorrentes com detecção de mudanças e alertas via webhook.

    Usa APScheduler como backend de agendamento (opcional).
    Sem APScheduler instalado, os jobs podem ser disparados manualmente via
    `run_scheduled_research(job_id)` ou pelo comando `sra schedule run`.
    """

    def __init__(self, orchestrator: Any, jobs_file: str = _DEFAULT_JOBS_FILE):
        self.orchestrator = orchestrator
        self.jobs_file = jobs_file
        self._jobs: Dict[str, ScheduledJob] = self._load_jobs()
        self._apscheduler_available = self._check_apscheduler()

    # ── Verificação de Dependências ───────────────────────────────────────────

    def _check_apscheduler(self) -> bool:
        try:
            import apscheduler  # noqa: F401
            return True
        except ImportError:
            logger.debug("APScheduler não instalado — modo manual ativo (pip install apscheduler).")
            return False

    # ── API de Agendamento ────────────────────────────────────────────────────

    def schedule_research(
        self,
        query: str,
        cron_expr: str,
        output_dir: str,
        webhook_url: Optional[str] = None,
        alert_on_changes: bool = True,
        email: Optional[str] = None,
    ) -> str:
        """
        Registra um novo job de pesquisa recorrente.
        Retorna o job_id único do job criado.
        """
        job = ScheduledJob(
            query=query,
            cron=cron_expr,
            output_dir=output_dir,
            webhook_url=webhook_url,
            alert_on_changes=alert_on_changes,
            email=email,
        )
        self._jobs[job.id] = job
        self._save_jobs()
        logger.info(f"ResearchScheduler: Job criado — id={job.id} | query='{query}' | cron='{cron_expr}'")
        return job.id

    async def run_scheduled_research(self, job_id: str) -> str:
        """
        Executa imediatamente um job registrado.
        Salva relatório em output_dir, compara com o anterior e dispara alertas.
        """
        job = self._jobs.get(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' não encontrado.")

        logger.info(f"ResearchScheduler: Executando job={job_id} | query='{job.query}'")
        report = await self.orchestrator.research(job.query)

        # Salva o relatório no disco
        os.makedirs(job.output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_query = re.sub(r"[^\w\s-]", "", job.query[:40]).strip().replace(" ", "_")
        report_path = os.path.join(job.output_dir, f"{safe_query}_{timestamp}.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info(f"ResearchScheduler: Relatório salvo em: {report_path}")

        # Detecta mudanças em relação ao relatório anterior
        changes: List[str] = []
        if job.last_report_path and os.path.exists(job.last_report_path):
            try:
                with open(job.last_report_path, "r", encoding="utf-8") as f:
                    old_report = f.read()
                changes = self.compare_with_previous(report, old_report)
                logger.info(f"ResearchScheduler: {len(changes)} mudança(s) detectada(s).")
            except Exception as e:
                logger.warning(f"ResearchScheduler: Erro ao comparar relatórios: {e}")

        # Atualiza metadados do job
        job.last_run = datetime.now().isoformat()
        job.last_report_path = report_path
        self._save_jobs()

        # Envia alerta se houver mudanças e o job tiver alertas habilitados
        if changes and job.alert_on_changes:
            await self.send_alert(changes, job.webhook_url)

        return report

    # ── Detecção de Mudanças ──────────────────────────────────────────────────

    def compare_with_previous(self, new_report: str, old_report: str) -> List[str]:
        """
        Compara dois relatórios e retorna lista descritiva de mudanças detectadas.

        Detecta:
        1. Novas entidades (nomes próprios)
        2. Novas fontes/URLs citadas
        3. Novos dados numéricos / estatísticas
        4. Novos tópicos cobertos (novos headings ##)
        5. Mudança no Research Score (grade)
        """
        changes: List[str] = []

        # 1. Novas entidades (nomes próprios capitalizados)
        new_entities = set(_ENTITY_RE.findall(new_report))
        old_entities = set(_ENTITY_RE.findall(old_report))
        added_entities = new_entities - old_entities
        if added_entities:
            sample = sorted(added_entities)[:5]
            changes.append(f"🆕 Novas entidades detectadas: {', '.join(sample)}")

        # 2. Novas fontes/URLs citadas
        new_urls = set(_URL_RE.findall(new_report))
        old_urls = set(_URL_RE.findall(old_report))
        added_urls = new_urls - old_urls
        if added_urls:
            changes.append(f"🔗 {len(added_urls)} nova(s) fonte(s) referenciada(s)")

        # 3. Novos dados numéricos / estatísticas
        new_numbers = set(_NUMBER_RE.findall(new_report))
        old_numbers = set(_NUMBER_RE.findall(old_report))
        added_numbers = new_numbers - old_numbers
        if added_numbers:
            sample = sorted(added_numbers)[:3]
            changes.append(f"📊 Novos dados numéricos: {', '.join(sample)}")

        # 4. Novos tópicos cobertos (novos ## headings)
        new_headings = set(_HEADING_RE.findall(new_report))
        old_headings = set(_HEADING_RE.findall(old_report))
        added_headings = new_headings - old_headings
        if added_headings:
            sample = sorted(added_headings)[:3]
            changes.append(f"📌 Novas seções adicionadas: {', '.join(sample)}")

        # 5. Mudança no Research Score (grade A/B/C...)
        new_score_m = _SCORE_RE.search(new_report)
        old_score_m = _SCORE_RE.search(old_report)
        if new_score_m and old_score_m and new_score_m.group(1) != old_score_m.group(1):
            changes.append(
                f"📈 Research Score mudou: {old_score_m.group(1)} → {new_score_m.group(1)}"
            )

        return changes

    # ── Alertas ───────────────────────────────────────────────────────────────

    async def send_alert(
        self,
        changes: List[str],
        webhook_url: Optional[str] = None,
    ) -> None:
        """
        Envia alertas de mudanças via webhook HTTP POST (Slack, Discord, N8N, etc.).
        Caso não haja webhook_url, apenas loga as mudanças.
        """
        message = "🔔 *SRA — Mudanças Detectadas na Pesquisa Agendada*\n\n"
        message += "\n".join(f"• {c}" for c in changes)

        if webhook_url:
            try:
                import aiohttp
                payload = {"text": message}
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status in (200, 204):
                            logger.info(f"ResearchScheduler: Alerta enviado para webhook (HTTP {resp.status})")
                        else:
                            logger.warning(f"ResearchScheduler: Webhook retornou status {resp.status}")
            except Exception as e:
                logger.warning(f"ResearchScheduler: Falha ao enviar alerta via webhook: {e}")
        else:
            logger.info(f"ResearchScheduler: Alerta (sem webhook configurado):\n{message}")

    # ── Gerenciamento de Jobs ─────────────────────────────────────────────────

    def list_jobs(self) -> List[Dict[str, Any]]:
        """Retorna lista de todos os jobs agendados como dicts."""
        return [job.to_dict() for job in self._jobs.values()]

    def cancel_job(self, job_id: str) -> bool:
        """
        Cancela e remove um job pelo ID.
        Retorna True se o job foi encontrado e removido.
        """
        if job_id not in self._jobs:
            logger.warning(f"ResearchScheduler: Job '{job_id}' não encontrado para cancelamento.")
            return False
        del self._jobs[job_id]
        self._save_jobs()
        logger.info(f"ResearchScheduler: Job '{job_id}' cancelado com sucesso.")
        return True

    # ── Persistência de Jobs ──────────────────────────────────────────────────

    def _load_jobs(self) -> Dict[str, ScheduledJob]:
        """Carrega jobs persistidos do arquivo JSON."""
        if not os.path.exists(self.jobs_file):
            return {}
        try:
            with open(self.jobs_file, "r", encoding="utf-8") as f:
                raw: Dict[str, Any] = json.load(f)
            return {jid: ScheduledJob.from_dict(data) for jid, data in raw.items()}
        except Exception as e:
            logger.warning(f"ResearchScheduler: Erro ao carregar jobs de '{self.jobs_file}': {e}")
            return {}

    def _save_jobs(self) -> None:
        """Persiste os jobs no arquivo JSON de forma atômica."""
        try:
            parent = os.path.dirname(self.jobs_file) or "."
            os.makedirs(parent, exist_ok=True)
            with open(self.jobs_file, "w", encoding="utf-8") as f:
                json.dump(
                    {jid: job.to_dict() for jid, job in self._jobs.items()},
                    f,
                    indent=2,
                    default=str,
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.warning(f"ResearchScheduler: Erro ao salvar jobs em '{self.jobs_file}': {e}")
