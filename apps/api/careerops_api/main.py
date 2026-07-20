from base64 import b64decode
from dataclasses import dataclass
from pathlib import Path
import os
import secrets
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from apps.api.careerops_api.schemas import (
    AnalyzeRequest,
    ApproveRequest,
    CreateApplicationRequest,
    MarkSubmittedRequest,
)
from careerops.analyzers.fit import analyze_fit
from careerops.core.models import Application, Approval, Report, ResumeVersion
from careerops.core.repository import CareerOpsRepository
from careerops.gates.risk import evaluate_risks
from careerops.parsers.jd_parser import parse_jd
from careerops.parsers.pdf_parser import parse_pdf
from careerops.renderers.latex import render_latex, sha256_file


DEFAULT_ALLOWED_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"]


@dataclass(frozen=True)
class BasicAuthConfig:
    username: str
    password: str


def default_repository() -> CareerOpsRepository:
    root = Path(__file__).resolve().parents[3]
    database_path = _configured_path("CAREEROPS_DB_PATH", root / "data" / "careerops.db", root)
    vault_path = _configured_path("CAREEROPS_VAULT_PATH", root / "vault", root)
    repo = CareerOpsRepository(database_path, vault_path)
    repo.initialize()
    return repo


def _configured_path(name: str, default: Path, root: Path) -> Path:
    raw = os.getenv(name)
    if not raw:
        return default
    path = Path(raw)
    return path if path.is_absolute() else root / path


def create_app(
    repository: CareerOpsRepository | None = None,
    *,
    allowed_origins: list[str] | None = None,
    auth: BasicAuthConfig | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    repo = repository or default_repository()
    root = Path(__file__).resolve().parents[3]
    app = FastAPI(title="CareerOps Local API")
    _add_basic_auth_middleware(app, auth if auth is not None else _auth_from_env())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins or _allowed_origins_from_env(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        try:
            with repo.connection() as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Database unavailable") from exc
        return {"status": "ok", "database": "ok"}

    @app.get("/applications")
    def list_applications() -> list[dict[str, Any]]:
        return [_application_payload(application) for application in repo.list_applications()]

    @app.post("/applications", status_code=201)
    def create_application(payload: CreateApplicationRequest) -> dict[str, Any]:
        application = repo.create_application(
            company=payload.company,
            role_title=payload.role_title,
            location=payload.location,
            source_url=payload.source_url,
            jd_text=payload.jd_text,
            deadline=payload.deadline,
            notes=payload.notes,
        )
        return _application_payload(application)

    @app.get("/applications/{application_id}")
    def get_application(application_id: str) -> dict[str, Any]:
        try:
            return _application_payload(repo.get_application(application_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Application not found") from exc

    @app.post("/applications/{application_id}/resume", status_code=201)
    async def upload_resume(
        application_id: str,
        name: str = Form(...),
        role_family: str = Form("general"),
        tex_file: UploadFile = File(...),
    ) -> dict[str, Any]:
        try:
            repo.get_application(application_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Application not found") from exc

        resume_dir = repo.vault_path / "resumes" / f"upload_{uuid4().hex}"
        resume_dir.mkdir(parents=True, exist_ok=True)
        tex_path = resume_dir / tex_file.filename
        content = await tex_file.read()
        tex_path.write_bytes(content)
        resume = repo.create_resume_version(
            name=name,
            role_family=role_family,
            tex_path=tex_path,
            tex_sha256=sha256_file(tex_path),
        )
        repo.select_resume(application_id, resume.id)
        return _resume_payload(resume)

    @app.post("/resumes/{resume_id}/render")
    def render_resume(resume_id: str) -> dict[str, Any]:
        try:
            resume = repo.get_resume_version(resume_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resume not found") from exc

        result = render_latex(resume.tex_path, repo.vault_path / "resumes" / resume.id / "render")
        if not result.success:
            updated = repo.update_resume_render(
                resume.id,
                pdf_path=None,
                pdf_sha256=None,
                render_status="failed",
                parse_status="not_parsed",
                page_count=None,
            )
            payload = _resume_payload(updated)
            payload["render_error"] = result.error_summary
            payload["render_log_path"] = str(result.log_path)
            return payload

        parsed = parse_pdf(result.pdf_path)
        updated = repo.update_resume_render(
            resume.id,
            pdf_path=result.pdf_path,
            pdf_sha256=result.pdf_sha256,
            render_status="rendered",
            parse_status="parsed",
            page_count=parsed.page_count,
        )
        return _resume_payload(updated)

    @app.post("/applications/{application_id}/analyze")
    def analyze_application(application_id: str, payload: AnalyzeRequest) -> dict[str, Any]:
        try:
            application = repo.get_application(application_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Application not found") from exc
        if not application.selected_resume_version_id:
            raise HTTPException(status_code=409, detail="No resume selected")

        resume = repo.get_resume_version(application.selected_resume_version_id)
        if not resume.pdf_path or not resume.pdf_sha256:
            raise HTTPException(status_code=409, detail="Resume PDF is missing")

        snapshot = repo.get_jd_snapshot(application.jd_snapshot_id)
        jd = parse_jd(snapshot.raw_text_path.read_text(encoding="utf-8"))
        parsed_pdf = parse_pdf(resume.pdf_path)
        pdf_is_stale = resume.tex_path.exists() and sha256_file(resume.tex_path) != resume.tex_sha256
        fit = analyze_fit(jd, parsed_pdf.text)
        risks = evaluate_risks(
            resume_text=parsed_pdf.text,
            claims=payload.claims,
            pdf_is_stale=pdf_is_stale,
            pdf_parse_ok=bool(parsed_pdf.text.strip()),
            page_count=parsed_pdf.page_count,
        )
        report = repo.save_report(
            application_id=application.id,
            resume_version_id=resume.id,
            jd_snapshot_id=snapshot.id,
            fit=fit.to_dict(),
            risks=risks.to_dict(),
        )
        return _report_payload(report)

    @app.post("/applications/{application_id}/approve")
    def approve_application(application_id: str, payload: ApproveRequest) -> dict[str, Any]:
        try:
            approval = repo.approve_application(application_id, payload.report_id, payload.note)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _approval_payload(approval)

    @app.post("/applications/{application_id}/mark-submitted")
    def mark_submitted(application_id: str, payload: MarkSubmittedRequest) -> dict[str, Any]:
        try:
            application = repo.mark_submitted_manual(application_id, payload.note)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _application_payload(application)

    _mount_static_app(app, static_dir if static_dir is not None else root / "apps" / "web" / "dist")
    return app


def _allowed_origins_from_env() -> list[str]:
    raw = os.getenv("CAREEROPS_ALLOWED_ORIGINS")
    if not raw:
        return DEFAULT_ALLOWED_ORIGINS
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or DEFAULT_ALLOWED_ORIGINS


def _auth_from_env() -> BasicAuthConfig | None:
    require_auth = os.getenv("CAREEROPS_REQUIRE_AUTH", "").lower() in {"1", "true", "yes", "on"}
    username = os.getenv("CAREEROPS_AUTH_USERNAME", "")
    password = os.getenv("CAREEROPS_AUTH_PASSWORD", "")
    if username and password:
        return BasicAuthConfig(username=username, password=password)
    if require_auth:
        raise RuntimeError("CAREEROPS_REQUIRE_AUTH is set but auth credentials are missing")
    return None


def _add_basic_auth_middleware(app: FastAPI, config: BasicAuthConfig | None) -> None:
    if config is None:
        return

    @app.middleware("http")
    async def require_basic_auth(request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        authorization = request.headers.get("Authorization", "")
        if _basic_auth_matches(authorization, config):
            return await call_next(request)
        return PlainTextResponse(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="CareerOps"'},
        )


def _basic_auth_matches(authorization: str, config: BasicAuthConfig) -> bool:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "basic" or not token:
        return False
    try:
        decoded = b64decode(token, validate=True).decode("utf-8")
    except Exception:
        return False
    username, separator, password = decoded.partition(":")
    if separator != ":":
        return False
    return secrets.compare_digest(username, config.username) and secrets.compare_digest(password, config.password)


def _mount_static_app(app: FastAPI, static_dir: Path) -> None:
    static_dir = Path(static_dir)
    index_path = static_dir / "index.html"
    if not index_path.exists():
        return
    assets_dir = static_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_react_app(full_path: str):
        candidate = (static_dir / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(static_dir.resolve()):
            return FileResponse(candidate)
        return FileResponse(index_path)


app = create_app()


def _application_payload(application: Application) -> dict[str, Any]:
    return {
        "id": application.id,
        "company": application.company,
        "role_title": application.role_title,
        "location": application.location,
        "source_url": application.source_url,
        "source_kind": application.source_kind,
        "jd_snapshot_id": application.jd_snapshot_id,
        "status": application.status.value,
        "selected_resume_version_id": application.selected_resume_version_id,
        "latest_report_id": application.latest_report_id,
        "approval_id": application.approval_id,
        "submission_state": application.submission_state.value,
        "deadline": application.deadline,
        "notes": application.notes,
        "created_at": application.created_at,
        "updated_at": application.updated_at,
    }


def _resume_payload(resume: ResumeVersion) -> dict[str, Any]:
    return {
        "id": resume.id,
        "name": resume.name,
        "role_family": resume.role_family,
        "tex_path": str(resume.tex_path),
        "tex_sha256": resume.tex_sha256,
        "pdf_path": str(resume.pdf_path) if resume.pdf_path else None,
        "pdf_sha256": resume.pdf_sha256,
        "parent_resume_version_id": resume.parent_resume_version_id,
        "render_status": resume.render_status,
        "parse_status": resume.parse_status,
        "page_count": resume.page_count,
        "created_at": resume.created_at,
        "updated_at": resume.updated_at,
    }


def _report_payload(report: Report) -> dict[str, Any]:
    return {
        "id": report.id,
        "application_id": report.application_id,
        "resume_version_id": report.resume_version_id,
        "jd_snapshot_id": report.jd_snapshot_id,
        "fit": report.fit,
        "risks": report.risks,
        "blocking_issue_count": report.blocking_issue_count,
        "warning_count": report.warning_count,
        "created_at": report.created_at,
    }


def _approval_payload(approval: Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "application_id": approval.application_id,
        "resume_version_id": approval.resume_version_id,
        "report_id": approval.report_id,
        "pdf_sha256": approval.pdf_sha256,
        "approved_at": approval.approved_at,
        "approval_note": approval.approval_note,
    }
