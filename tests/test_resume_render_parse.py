from pathlib import Path

from careerops.parsers.pdf_parser import parse_pdf
from careerops.renderers.latex import find_latex_tool, render_latex


def test_renderer_discovers_local_latex_tool():
    tool = find_latex_tool()

    assert tool is not None
    assert tool.name in {"latexmk", "pdflatex"}
    assert tool.executable.exists()


def test_render_latex_and_parse_pdf_text(tmp_path: Path):
    tex_path = tmp_path / "sample_resume.tex"
    tex_path.write_text(Path("fixtures/sample_resume.tex").read_text(encoding="utf-8"), encoding="utf-8")

    result = render_latex(tex_path, tmp_path / "rendered")
    parsed = parse_pdf(result.pdf_path)

    assert result.success is True
    assert result.pdf_path.exists()
    assert result.log_path.exists()
    assert len(result.pdf_sha256) == 64
    assert parsed.page_count == 1
    assert "Nishant Harkut" in parsed.text
    assert "QuickIntell" in parsed.text
    assert "PocketBuddy" in parsed.text


def test_render_latex_returns_clear_failure_for_broken_source(tmp_path: Path):
    tex_path = tmp_path / "broken.tex"
    tex_path.write_text("\\documentclass{article}\\begin{document}\\badcommand\\end{document}", encoding="utf-8")

    result = render_latex(tex_path, tmp_path / "broken-render")

    assert result.success is False
    assert result.pdf_path is None
    assert result.log_path.exists()
    assert "failed" in result.error_summary.lower() or "error" in result.error_summary.lower()
