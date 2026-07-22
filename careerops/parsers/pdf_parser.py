from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True)
class ParsedPDF:
    path: Path
    page_count: int
    text: str


def parse_pdf(pdf_path: Path | None) -> ParsedPDF:
    if pdf_path is None:
        raise ValueError("PDF path is missing")
    path = Path(pdf_path)
    reader = PdfReader(str(path))
    page_text = [page.extract_text() or "" for page in reader.pages]
    return ParsedPDF(path=path, page_count=len(reader.pages), text="\n".join(page_text))


def parse_pdf_bytes(content: bytes, *, path: Path | None = None) -> ParsedPDF:
    display_path = Path(path) if path is not None else Path("<memory>")
    reader = PdfReader(BytesIO(content))
    page_text = [page.extract_text() or "" for page in reader.pages]
    return ParsedPDF(
        path=display_path,
        page_count=len(reader.pages),
        text="\n".join(page_text),
    )
