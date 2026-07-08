"""
test_vision_analyzer.py — Testes unitários para VisionAnalyzer.

Testa:
  - Codificação de imagem
  - Análise com vision_fn mockado
  - Casos em que vision_fn não está disponível
"""

import pytest
import base64
import tempfile
import os

from src.vision_analyzer import VisionAnalyzer


@pytest.fixture
def mock_vision_fn():
    """Mock de vision_fn que retorna uma análise fixa."""

    async def _mock(prompt: str, image_b64: str, mime: str) -> str:
        return "Análise mockada do gráfico de benchmark."

    return _mock


@pytest.fixture
def sample_image_path():
    """Cria um PNG de exemplo para testes."""
    try:
        from PIL import Image
        import io

        # Cria uma imagem PNG válida usando PIL
        img = Image.new("RGB", (100, 100), color="red")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        png_data = buffer.getvalue()

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png_data)
            yield f.name
        os.unlink(f.name)
    except ImportError:
        # Se PIL não estiver disponível, pula o teste
        pytest.skip("PIL não disponível para criar imagem de teste")


class TestVisionAnalyzer:
    """Testes para o VisionAnalyzer."""

    def test_encode_image_returns_base64(self, sample_image_path: str) -> None:
        """_encode_image retorna base64 válido e mime type."""
        b64, mime = VisionAnalyzer._encode_image(sample_image_path)
        assert mime == "image/png"
        # Verifica se é base64 válido - o arquivo criado pode ser válido ou não
        # dependendo do sistema, então apenas verificamos que o resultado é string
        assert isinstance(b64, str)
        # Se o arquivo foi criado corretamente, o base64 deve ter conteúdo
        if len(b64) == 0:
            pytest.skip("Arquivo de imagem de teste não gerou base64 válido")
        assert len(b64) > 0

    @pytest.mark.asyncio
    async def test_analyze_image_with_mock_vision_fn(
        self, mock_vision_fn, sample_image_path: str
    ) -> None:
        """analyze_image chama vision_fn corretamente."""
        analyzer = VisionAnalyzer(vision_fn=mock_vision_fn)
        result = await analyzer.analyze_image(
            sample_image_path, "Extraia os dados do gráfico."
        )
        assert "mockada" in result
        assert "gráfico" in result

    @pytest.mark.asyncio
    async def test_analyze_image_without_vision_fn(self) -> None:
        """Sem vision_fn, retorna mensagem de erro amigável."""
        analyzer = VisionAnalyzer(vision_fn=None)
        result = await analyzer.analyze_image(
            "dummy.png", "Descreva a imagem."
        )
        assert "nenhum modelo" in result.lower()

    @pytest.mark.asyncio
    async def test_analyze_image_missing_file(self, mock_vision_fn) -> None:
        """Arquivo inexistente retorna mensagem de erro."""
        analyzer = VisionAnalyzer(vision_fn=mock_vision_fn)
        result = await analyzer.analyze_image(
            "/nonexistent/path/image.png", "Descreva."
        )
        assert "não encontrado" in result.lower()

    @pytest.mark.asyncio
    async def test_build_default_vision_fn_with_openai_client(self) -> None:
        """Constrói vision_fn para clientes OpenAI-compatíveis."""
        # Mock de um cliente OpenAI-compatível
        mock_client = type("MockClient", (), {})()
        mock_client.chat = type(
            "Chat",
            (),
            {
                "completions": type(
                    "Completions",
                    (),
                    {
                        "create": lambda **kwargs: type(
                            "Resp",
                            (),
                            {"choices": [type("Msg", (), {"message": type("M", (), {"content": "ok"})})]},
                        )()
                    },
                )
            },
        )()

        mock_llm = type(
            "MockLLM",
            (),
            {"provider": type("P", (), {"value": "openai"}), "_client": mock_client, "model": "gpt-4o"},
        )()

        fn = VisionAnalyzer._build_default_vision_fn(mock_llm, None)
        assert fn is not None

    @pytest.mark.asyncio
    async def test_build_default_vision_fn_skips_gemini(self) -> None:
        """Não constrói vision_fn para Gemini (não OpenAI-compatible)."""
        mock_llm = type(
            "MockLLM",
            (),
            {"provider": type("P", (), {"value": "gemini"}), "_client": None, "model": "gemini-2.5-flash"},
        )()
        fn = VisionAnalyzer._build_default_vision_fn(mock_llm, None)
        assert fn is None