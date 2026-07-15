"""Smoke tests do Dashboard de Qualidade (Bloco 12 / E5-T1) no Streamlit.

O módulo ``ui/streamlit_app.py`` importa ``streamlit`` globalmente (não dá
para rodar em browser no pytest), então os helpers são exercitados com
``streamlit`` real disponível mas com os primitivos de renderização mockados.
Cobrem: gauger de qualidade com cores, painel RAGAS a partir do
``quality_gate_result`` (Bloco 6), e exportação .md/.pdf/.docx (via stubs).
"""

import sys
import types

import pytest

# Garante que `streamlit` exista no sys.modules (real ou stub leve) antes do import.
if "streamlit" not in sys.modules:
    try:
        import streamlit  # noqa: F401  (real, se instalado)
    except Exception:  # pragma: no cover - fallback mínimo
        _st = types.ModuleType("streamlit")
        _st.markdown = lambda *a, **k: None
        _st.metric = lambda *a, **k: None
        _st.columns = lambda *a, **k: []
        _st.caption = lambda *a, **k: None
        _st.info = lambda *a, **k: None
        _st.download_button = lambda *a, **k: None
        sys.modules["streamlit"] = _st


@pytest.fixture()
def app():
    import ui.streamlit_app as m

    return m


class _StubCtx:
    def __init__(self, qg):
        self.extra = {"quality_gate_result": qg}


class _StubQG:
    def __init__(self, faith, rel, trac, mode="proxy", passed=True):
        self.faithfulness = faith
        self.relevancy = rel
        self.traceability = trac
        self.mode = mode
        self.passed = passed
        self.retry_recommended = not passed


def test_quality_gauge_handles_none(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.st, "metric", lambda *a, **k: calls.append(a))
    app._quality_gauge("Faithfulness", None, 0.70)
    assert calls and "N/A" in calls[0]


def test_quality_gauge_colors(app, monkeypatch):
    rendered = []
    monkeypatch.setattr(
        app.st, "markdown", lambda s, *a, **k: rendered.append(s)
    )
    # Verde: >= threshold
    app._quality_gauge("Faithfulness", 0.95, 0.70)
    assert "#22c55e" in rendered[-1]
    # Amarelo: >= 80% do threshold
    app._quality_gauge("Faithfulness", 0.60, 0.70)
    assert "#f59e0b" in rendered[-1]
    # Vermelho: abaixo
    app._quality_gauge("Faithfulness", 0.20, 0.70)
    assert "#ef4444" in rendered[-1]


def test_render_quality_dashboard_with_qg(app, monkeypatch):
    out = []
    monkeypatch.setattr(app.st, "markdown", lambda s, *a, **k: out.append(s))
    monkeypatch.setattr(app.st, "caption", lambda s, *a, **k: out.append(s))

    ctx = _StubCtx(_StubQG(0.85, 0.80, 0.90, mode="proxy", passed=True))
    app.render_quality_dashboard(ctx)
    joined = "\n".join(out)
    assert "Qualidade desta pesquisa" in joined
    assert "proxy" in joined
    assert "Aprovado" in joined


def test_render_quality_dashboard_missing_qg(app, monkeypatch):
    out = []
    monkeypatch.setattr(app.st, "info", lambda s, *a, **k: out.append(s))
    app.render_quality_dashboard(_StubCtx(None))
    assert any("Quality Gate" in s for s in out)


def test_render_quality_dashboard_none_ctx(app, monkeypatch):
    # Não deve levantar com ctx=None.
    app.render_quality_dashboard(None)


def test_export_markdown_button(app, monkeypatch):
    args = {}
    monkeypatch.setattr(
        app.st,
        "download_button",
        lambda *a, **k: args.update(k),
    )
    app.export_report_markdown("# Título\n\ncorpo", "benchmark python")
    assert args.get("mime") == "text/markdown"
    assert args["file_name"].endswith(".md")


def test_export_pdf_docx_buttons(app, monkeypatch):
    calls = []

    def fake_dl(*a, **k):
        calls.append(k.get("mime"))

    monkeypatch.setattr(app.st, "download_button", fake_dl)

    class _Col:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(app.st, "columns", lambda n: [_Col(), _Col()])
    app.export_report_pdf_docx("# Relatório\n\ntexto", "teste query")
    # Deve tentar registrar ao menos .pdf e .docx (libs presentes no venv).
    assert any("application/pdf" in c for c in calls)
    assert any("wordprocessingml" in c for c in calls)
