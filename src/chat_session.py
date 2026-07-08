"""
chat_session.py — Chat com a Pesquisa (RAG Conversacional Pós-Relatório) - Fase 2.

Permite perguntas de acompanhamento ancoradas na pesquisa já feita.
Diferente do HITLDialogAgent (que serve só para aprovações pontuais durante o
pipeline), este é um loop de conversa pós-relatório que carrega o PipelineContext
final + EvidenceGraph como contexto de RAG.

Uso:
    from src.chat_session import ChatSession
    chat = ChatSession(orchestrator=orch)
    chat.start_session(session_id="abc123", query="Rust async")
    answer = chat.ask("qual o benchmark de performance?")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("chat_session")


@dataclass
class ChatMessage:
    """Uma mensagem no histórico de chat."""

    role: str  # "user" ou "assistant"
    content: str
    sources: list[str] = field(default_factory=list)


class ChatSession:
    """
    Sessão de chat RAG pós-relatório.

    Constrói o contexto a partir do PipelineContext final + EvidenceGraph
    e responde perguntas usando o LLM com memória de conversa.
    """

    def __init__(self, orchestrator: Any = None, llm_client: Any = None) -> None:
        self.orchestrator = orchestrator
        self.llm = llm_client or (orchestrator.llm if orchestrator else None)
        self.session_id: str | None = None
        self.query_topic: str = ""
        self.context: dict[str, Any] = {}
        self.history: list[ChatMessage] = []

    def start_session(
        self,
        session_id: str,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Inicia uma nova sessão de chat com o contexto da pesquisa."""
        self.session_id = session_id
        self.query_topic = query
        self.context = context or {}
        self.history.clear()
        logger.info(
            "ChatSession: sessão iniciada para '%s' (session=%s)", query, session_id
        )

    def ask(self, question: str) -> str:
        """
        Responde a uma pergunta usando o contexto da pesquisa.

        Args:
            question: Pergunta do usuário.

        Returns:
            Resposta gerada com base no contexto do relatório.
        """
        if not self.llm:
            return "Desculpe, o cliente LLM não está disponível para processar sua pergunta."

        # Constrói o prompt com contexto e histórico
        prompt = self._build_prompt(question)

        try:
            response = self.llm.generate(
                prompt,
                temperature=0.3,
                max_tokens=1024,
            )
            self.history.append(ChatMessage(role="user", content=question))
            self.history.append(ChatMessage(role="assistant", content=response))
            return response
        except Exception as e:
            logger.error("ChatSession: erro ao gerar resposta: %s", e)
            return f"Erro ao processar sua pergunta: {e}"

    def _build_prompt(self, question: str) -> str:
        """Constroi o prompt completo com contexto de pesquisa e histórico."""
        # Extrai informações relevantes do contexto
        report = self.context.get("report", "")
        evidence_graph = self.context.get("evidence_graph", {})
        ranked_results = self.context.get("ranked_results", [])

        # Formata as fontes
        sources = [r.get("url", "") for r in ranked_results[:5] if r.get("url")]
        source_list = (
            "\n".join(f"- {s}" for s in sources)
            if sources
            else "- Nenhuma fonte específica"
        )

        # Formata o histórico
        history_text = "\n".join(
            f"{m.role.upper()}: {m.content}" for m in self.history[-6:]
        )

        # Monta o prompt completo
        prompt = (
            "Você é um assistente de pesquisa especializado. "
            "Use as informações de contexto abaixo para responder à pergunta do usuário.\n\n"
            "--- CONTEXTO DA PESQUISA ---\n"
            f"Tópico original: {self.query_topic}\n"
            f"Relatório: {report[:3000]}...\n\n"
            "Fontes principais:\n"
            f"{source_list}\n\n"
            "--- HISTÓRICO DA CONVERSA ---\n"
            f"{history_text}\n\n"
            "--- PERGUNTA DO USUÁRIO ---\n"
            f"{question}\n\n"
            "Responda de forma clara, citando as fontes quando relevante. "
            "Se a informação não estiver no contexto, diga que não foi encontrada."
        )
        return prompt

    def get_history(self) -> list[dict[str, str]]:
        """Retorna o histórico como lista de dicionários."""
        return [
            {"role": m.role, "content": m.content, "sources": m.sources}
            for m in self.history
        ]
