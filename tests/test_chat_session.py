"""
test_chat_session.py — Testes unitários para o ChatSession (RAG conversacional).

Testa:
  - Inicialização de sessão
  - Geração de resposta com LLM mockado
  - Construção de prompt com contexto
  - Histórico de conversa
"""

import pytest
from unittest.mock import MagicMock

from src.chat_session import ChatSession, ChatMessage


@pytest.fixture
def mock_llm() -> MagicMock:
    """Mock de LLMClient que retorna resposta fixa."""
    mock = MagicMock()
    mock.generate.return_value = (
        "Com base na pesquisa sobre Rust async, os benchmarks mostram que "
        "tokio supera async-std em throughput."
    )
    return mock


class TestChatSession:
    """Testes para o ChatSession."""

    def test_session_starts_with_context(self, mock_llm: MagicMock) -> None:
        """Sessão inicia corretamente com query e contexto."""
        chat = ChatSession(llm_client=mock_llm)
        chat.start_session(
            session_id="test123",
            query="Rust async performance",
            context={"report": "Relatório sobre Rust...", "ranked_results": []},
        )

        assert chat.session_id == "test123"
        assert chat.query_topic == "Rust async performance"
        assert "report" in chat.context

    def test_ask_generates_response(self, mock_llm: MagicMock) -> None:
        """ask() retorna resposta gerada pelo LLM."""
        chat = ChatSession(llm_client=mock_llm)
        chat.start_session(session_id="s1", query="Rust async")

        question = "Qual framework tem melhor performance?"
        answer = chat.ask(question)

        assert "tokio" in answer
        mock_llm.generate.assert_called_once()

    def test_ask_without_llm_returns_error(self) -> None:
        """Sem LLM, retorna mensagem de erro clara."""
        chat = ChatSession(llm_client=None)
        chat.start_session(session_id="s2", query="Test")

        answer = chat.ask("Pergunta qualquer")
        assert "não está disponível" in answer

    def test_history_accumulates(self, mock_llm: MagicMock) -> None:
        """Histórico acumula mensagens user/assistant."""
        chat = ChatSession(llm_client=mock_llm)
        chat.start_session(session_id="s3", query="Test")

        chat.ask("Pergunta 1")
        chat.ask("Pergunta 2")

        history = chat.get_history()
        assert len(history) == 4  # 2 user + 2 assistant
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    def test_prompt_includes_context(self, mock_llm: MagicMock) -> None:
        """Prompt inclui relatório e fontes do contexto."""
        chat = ChatSession(llm_client=mock_llm)
        chat.start_session(
            session_id="s4",
            query="Python performance",
            context={
                "report": "Relatório detalhado sobre Python...",
                "ranked_results": [
                    {"url": "https://example.com/artigo1"},
                    {"url": "https://example.com/artigo2"},
                ],
            },
        )

        # Chama ask que internamente constrói o prompt
        chat.ask("Explique o benchmark")

        # Verifica que o prompt passado ao LLM contém o contexto
        call_args = mock_llm.generate.call_args[0][0]
        assert "Relatório detalhado" in call_args
        assert "example.com/artigo1" in call_args

    def test_multiple_questions_same_session(self, mock_llm: MagicMock) -> None:
        """Múltiplas perguntas na mesma sessão mantêm contexto."""
        chat = ChatSession(llm_client=mock_llm)
        chat.start_session(session_id="s5", query="Machine Learning")

        answer1 = chat.ask("O que é overfitting?")
        answer2 = chat.ask("Como evitar?")

        assert answer1
        assert answer2
        assert mock_llm.generate.call_count == 2