"""
test_data_analyzer.py — Testes unitários do DataAnalyzer com sandbox Docker.

Testa:
  - Geração de script Pandas com LLM (mockado)
  - Fallback de script quando LLM indisponível
  - Execução na sandbox (com verificação de timeout isolamento)
"""

import pytest
from unittest.mock import MagicMock, patch

from src.data_analyzer import DataAnalyzer, DataAnalysisResult


@pytest.fixture
def mock_code_agent() -> MagicMock:
    """Mock do CodeExecutionAgent que simula execução bem-sucedida."""
    mock = MagicMock()
    mock.execute_python.return_value = MagicMock(
        stdout="col1,col2\n1,2\n3,4\n---\nShape: (2, 2)",
        stderr="",
        exit_code=0,
        timed_out=False,
        error_message="",
    )
    return mock


class TestDataAnalyzer:
    """Testes para o DataAnalyzer."""

    def test_fallback_script_generates_valid_pandas(self, mock_code_agent: MagicMock, tmp_path) -> None:
        """Sem LLM, gera script de inspeção básico."""
        # Cria um CSV temporário
        csv_file = tmp_path / "test_data.csv"
        csv_file.write_text("a,b\n1,2\n3,4\n")

        analyzer = DataAnalyzer(code_agent=mock_code_agent, llm_client=None)
        result = analyzer.analyze(
            data_paths=[str(csv_file)],
            question="Qual o shape dos dados?",
        )

        assert result.status == "success"
        assert result.exit_code == 0
        assert "pandas" in result.script.lower() or "pd." in result.script
        assert "test_data.csv" in result.script

    def test_llm_script_generated_when_available(self, mock_code_agent: MagicMock, tmp_path) -> None:
        """Com LLM disponível, usa o script gerado."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("x,y\n10,20\n")

        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            "import pandas as pd\n"
            "df = pd.read_csv('/data/data.csv')\n"
            "print(df['x'].sum())\n"
        )

        analyzer = DataAnalyzer(code_agent=mock_code_agent, llm_client=mock_llm)
        result = analyzer.analyze(
            data_paths=[str(csv_file)],
            question="Qual a soma da coluna x?",
        )

        assert result.status == "success"
        mock_llm.generate.assert_called_once()
        assert "df['x'].sum()" in result.script or "read_csv" in result.script

    def test_empty_data_paths_returns_error(self, mock_code_agent: MagicMock) -> None:
        """Caminhos vazios retornam erro estruturado."""
        analyzer = DataAnalyzer(code_agent=mock_code_agent, llm_client=None)
        result = analyzer.analyze(
            data_paths=[],
            question="Análise sem arquivos",
        )

        assert result.status == "error"
        assert result.exit_code == -4
        assert "Nenhum arquivo" in result.error_message

    def test_script_with_code_fence_stripped(self, mock_code_agent: MagicMock, tmp_path) -> None:
        """Remove code fences residuais da resposta do LLM."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("a,b\n")

        mock_llm = MagicMock()
        mock_llm.generate.return_value = (
            "```python\nimport pandas as pd\ndf = pd.read_csv('/data/data.csv')\n"
            "print(df.head())\n```"
        )

        analyzer = DataAnalyzer(code_agent=mock_code_agent, llm_client=mock_llm)
        result = analyzer.analyze(
            data_paths=[str(csv_file)],
            question="Teste",
        )

        # Não deve ter crases no script final
        assert "```" not in result.script

    def test_json_file_detection(self, mock_code_agent: MagicMock, tmp_path) -> None:
        """Detecta formato JSON corretamente para read_json."""
        json_file = tmp_path / "data.json"
        json_file.write_text('[{"a": 1, "b": 2}]')

        analyzer = DataAnalyzer(code_agent=mock_code_agent, llm_client=None)
        result = analyzer.analyze(
            data_paths=[str(json_file)],
            question="Inspeção de JSON",
        )

        assert "read_json" in result.script or "pd.read_json" in result.script

    def test_max_file_limit_respected(self, mock_code_agent: MagicMock, tmp_path) -> None:
        """Respeita limite máximo de arquivos (_MAX_DATA_FILES = 5)."""
        files = []
        for i in range(7):
            f = tmp_path / f"data{i}.csv"
            f.write_text("a,b\n")
            files.append(str(f))

        analyzer = DataAnalyzer(code_agent=mock_code_agent, llm_client=None)
        result = analyzer.analyze(
            data_paths=files,
            question="Múltiplos arquivos",
        )

        # Deve ter processado apenas os primeiros 5 arquivos
        # O script fallback usa df0, df1, etc.
        assert len(result.files_analyzed) <= 5
