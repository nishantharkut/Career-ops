from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import subprocess


@dataclass(frozen=True)
class LatexTool:
    name: str
    executable: Path


@dataclass(frozen=True)
class RenderResult:
    success: bool
    pdf_path: Path | None
    log_path: Path
    pdf_sha256: str | None
    error_summary: str
    tool_name: str | None


def find_latex_tool() -> LatexTool | None:
    for name in ("latexmk", "pdflatex"):
        executable = shutil.which(name)
        if executable:
            return LatexTool(name=name, executable=Path(executable))
    return None


def render_latex(tex_path: Path, output_dir: Path) -> RenderResult:
    tex_path = Path(tex_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "render.log"
    tool = find_latex_tool()
    if tool is None:
        log_path.write_text("No local LaTeX tool found. Install latexmk or pdflatex.", encoding="utf-8")
        return RenderResult(False, None, log_path, None, "No local LaTeX tool found", None)

    command = _build_command(tool, tex_path, output_dir)
    completed = subprocess.run(
        command,
        cwd=tex_path.parent,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    log_text = "\n".join(
        [
            f"Command: {' '.join(command)}",
            f"Exit code: {completed.returncode}",
            "",
            "STDOUT:",
            completed.stdout,
            "",
            "STDERR:",
            completed.stderr,
        ]
    )
    log_path.write_text(log_text, encoding="utf-8", errors="replace")
    pdf_path = output_dir / f"{tex_path.stem}.pdf"
    if completed.returncode != 0 or not pdf_path.exists():
        return RenderResult(
            success=False,
            pdf_path=None,
            log_path=log_path,
            pdf_sha256=None,
            error_summary=_summarize_failure(completed.stdout, completed.stderr),
            tool_name=tool.name,
        )

    return RenderResult(
        success=True,
        pdf_path=pdf_path,
        log_path=log_path,
        pdf_sha256=sha256_file(pdf_path),
        error_summary="",
        tool_name=tool.name,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_command(tool: LatexTool, tex_path: Path, output_dir: Path) -> list[str]:
    if tool.name == "latexmk":
        return [
            str(tool.executable),
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-outdir={output_dir}",
            str(tex_path),
        ]
    return [
        str(tool.executable),
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-output-directory={output_dir}",
        str(tex_path),
    ]


def _summarize_failure(stdout: str, stderr: str) -> str:
    combined = "\n".join([stdout, stderr]).splitlines()
    for line in combined:
        lowered = line.lower()
        if "error" in lowered or "undefined control sequence" in lowered:
            return f"LaTeX render failed: {line.strip()}"
    return "LaTeX render failed. Open the render log."
