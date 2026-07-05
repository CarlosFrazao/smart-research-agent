"""Módulo gerenciador de Human-in-the-Loop (HITL) para o SRA.

Permite suspender a execução assíncrona de uma pesquisa para aguardar
aprovação ou modificação humana de artefatos críticos (outline, queries, links).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("hitl-manager")


class HITLManager:
    """Gerencia estados de suspensão assíncrona e aprovações humanas.

    Usa asyncio.Event de forma isolada por sessão para bloquear tarefas de
    forma assíncrona no pipeline sem interromper outras sessões do processo.
    """

    def __init__(self) -> None:
        self._events: Dict[str, asyncio.Event] = {}
        self._requests: Dict[str, Dict[str, Any]] = {}
        self._responses: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def request_approval(
        self,
        session_id: str,
        request_type: str,
        data: Any,
        timeout: float = 300.0,
    ) -> Any:
        """Solicita a aprovação de um artefato e bloqueia a execução até a resposta ou timeout.

        Args:
            session_id: Identificador único da sessão de pesquisa.
            request_type: Tipo da solicitação (ex: "outline", "queries", "links").
            data: Os dados originais a serem validados/modificados.
            timeout: Tempo limite em segundos para aguardar a resposta humana.

        Returns:
            Any: Os dados finais aprovados (que podem ser os mesmos originais,
                modificados pelo humano, ou os originais em caso de timeout).
        """
        async with self._lock:
            event = asyncio.Event()
            self._events[session_id] = event
            self._requests[session_id] = {
                "request_type": request_type,
                "data": data,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "timeout": timeout,
            }
            # Limpa qualquer resposta anterior residual
            self._responses.pop(session_id, None)

        logger.info(
            f"[HITL] Sessão '{session_id}' suspensa aguardando aprovação de '{request_type}' (timeout={timeout}s)."
        )

        try:
            # Aguarda a ativação do evento pelo endpoint de resposta com timeout
            await asyncio.wait_for(event.wait(), timeout=timeout)

            async with self._lock:
                approved_data = self._responses.get(session_id, data)
                logger.info(f"[HITL] Sessão '{session_id}' liberada pelo usuário.")
                return approved_data

        except asyncio.TimeoutError:
            logger.warning(
                f"[HITL] Timeout de {timeout}s estourado para a sessão '{session_id}'. Prosseguindo em modo automático."
            )
            return data

        finally:
            await self.cleanup_session(session_id)

    async def submit_response(self, session_id: str, approved_data: Any) -> bool:
        """Submete a resposta do usuário e libera a tarefa suspensa.

        Args:
            session_id: Identificador da sessão de pesquisa pendente.
            approved_data: Os dados finais validados ou editados pelo usuário.

        Returns:
            bool: True se a sessão foi liberada com sucesso, False caso não estivesse pendente.
        """
        async with self._lock:
            event = self._events.get(session_id)
            if not event:
                logger.warning(
                    f"[HITL] Nenhuma solicitação pendente encontrada para a sessão '{session_id}'."
                )
                return False

            self._responses[session_id] = approved_data
            event.set()
            return True

    async def cleanup_session(self, session_id: str) -> None:
        """Limpa as estruturas de controle de uma sessão concluída ou abortada."""
        async with self._lock:
            self._events.pop(session_id, None)
            self._requests.pop(session_id, None)
            self._responses.pop(session_id, None)

    def get_pending_request(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Recupera metadados do pedido de aprovação pendente de uma sessão."""
        return self._requests.get(session_id)

    def list_pending_requests(self) -> Dict[str, Dict[str, Any]]:
        """Lista todas as solicitações de aprovação ativas no momento."""
        return dict(self._requests)
