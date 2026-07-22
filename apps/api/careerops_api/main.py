from __future__ import annotations

from base64 import b64decode
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
from typing import Annotated, Any
from uuid import uuid4

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Path as PathParameter,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from apps.api.careerops_api.schemas import (
    AnalyzeRequest,
    ApproveRequest,
    CreateAiTaskRequest,
    CreateApplicationRequest,
    CreateResumeDraftRequest,
    EvidenceRequest,
    FindingDecisionRequest,
    MarkSubmittedRequest,
    ProfileFactRequest,
    RequirementsUpdateRequest,
)
from apps.api.careerops_api.task_runner import AiTaskRunner
from careerops.ai.contracts import ProviderAvailability, ProviderRequest
from careerops.ai.orchestrator import AiOrchestrator
from careerops.ai.providers.base import AiProvider
from careerops.ai.providers.codex import discover_codex_providers
from careerops.ai.providers.deterministic import DeterministicProvider
from careerops.ai.providers.ollama import OllamaProvider
from careerops.analyzers.fit import analyze_fit
from careerops.core.models import (
    AiArtifact,
    AiFinding,
    AiProviderConfig,
    AiTask,
    Application,
    ApplicationVersion,
    Approval,
    EvidenceItem,
    JobDescriptionSnapshot,
    ProfileFact,
    Report,
    ResumeVersion,
)
from careerops.core.repository import (
    ApplicationPacketSnapshot,
    CareerOpsRepository,
    PersistedJsonSizeError,
    redact,
)
from careerops.gates.risk import evaluate_risks
from careerops.parsers.jd_parser import parse_jd
from careerops.parsers.pdf_parser import parse_pdf
from careerops.renderers.latex import render_latex, sha256_file
from careerops.resumes.drafts import create_verified_resume_draft


DEFAULT_ALLOWED_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"]
MAX_RESUME_TEX_BYTES = 2_000_000
MAX_JD_DOWNLOAD_BYTES = 2_000_000
MAX_RESUME_PDF_DOWNLOAD_BYTES = 32_000_000
_SAFE_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
ApplicationId = Annotated[str, PathParameter(pattern=rf"^app_{_SAFE_ID}$")]
ResumeId = Annotated[str, PathParameter(pattern=rf"^res_{_SAFE_ID}$")]
TaskId = Annotated[str, PathParameter(pattern=rf"^ait_{_SAFE_ID}$")]
FindingId = Annotated[str, PathParameter(pattern=rf"^aif_{_SAFE_ID}$")]


@dataclass(frozen=True)
class BasicAuthConfig:
    username: str
    password: str


class _ArtifactPathError(ValueError):
    pass


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


def _provider_binding(status: ProviderAvailability) -> dict[str, Any]:
    values = status.model_dump(mode="json")
    values.pop("available")
    values.pop("detail")
    return values


def _register_provider(
    repository: CareerOpsRepository,
    registry: dict[str, AiProvider],
    provider: AiProvider,
    *,
    display_name: str,
    enabled: bool | None = None,
) -> AiProviderConfig:
    status = provider.availability()
    config = repository.upsert_ai_provider_config(
        provider_kind=status.provider_kind,
        display_name=display_name,
        privacy_mode=status.privacy_mode.value,
        endpoint=status.endpoint,
        identity_name=status.identity_name,
        model=status.model,
        enabled=status.available if enabled is None else enabled,
        availability_binding=_provider_binding(status),
    )
    registry[config.id] = provider
    return config


def _default_deterministic_output(request: ProviderRequest) -> Mapping[str, Any]:
    if request.task_type == "analyze_jd":
        context = json.loads(request.context_text())
        job_description = context.get("job_description", {})
        text = job_description.get("text", "") if isinstance(job_description, dict) else ""
        parsed = parse_jd(text if isinstance(text, str) else "")
        return {
            "task_type": "analyze_jd",
            "normalized_title": parsed.role_title,
            "normalized_company": parsed.company,
            "normalized_location": parsed.location,
            "responsibilities": parsed.responsibilities,
            "required_skills": parsed.required_skills,
            "preferred_skills": parsed.preferred_skills,
            "keywords": parsed.exact_keywords,
            "uncertainties": [],
        }
    if request.task_type == "analyze_fit":
        return {
            "task_type": "analyze_fit",
            "score": 0,
            "matches": [],
            "gaps": [],
            "risks": ["Human review is required"],
            "recommendations": ["Run a reviewed fit assessment"],
        }
    if request.task_type == "suggest_resume_edits":
        return {"task_type": "suggest_resume_edits", "suggestions": []}
    if request.task_type == "draft_cover_letter":
        return {
            "task_type": "draft_cover_letter",
            "plain_text": "Human-authored cover letter required.",
            "evidence_refs": [],
        }
    if request.task_type == "review_claims":
        return {
            "task_type": "review_claims",
            "decisions": [
                {
                    "claim_id": claim_id,
                    "decision": "needs_review",
                    "reason": "Human verification is required",
                    "evidence_refs": [],
                }
                for claim_id in request.expected_claim_ids
            ],
        }
    raise ValueError("Unsupported deterministic task")


def _default_provider_registry(
    repository: CareerOpsRepository,
    environment: Mapping[str, str] | None = None,
) -> dict[str, AiProvider]:
    environment = dict(os.environ if environment is None else environment)
    registry: dict[str, AiProvider] = {}
    deterministic = DeterministicProvider(_default_deterministic_output)
    _register_provider(
        repository,
        registry,
        deterministic,
        display_name="Deterministic verifier",
        enabled=True,
    )

    ollama = OllamaProvider(
        endpoint=environment.get("CAREEROPS_OLLAMA_URL")
        or "http://127.0.0.1:11434",
        model=environment.get("CAREEROPS_OLLAMA_MODEL") or "qwen2.5:7b",
    )
    _register_provider(
        repository,
        registry,
        ollama,
        display_name=f"Ollama {ollama.model}",
        enabled=False,
    )

    if environment.get("CAREEROPS_CODEX_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        for provider in discover_codex_providers(environment=environment):
            identity_name = provider.availability().identity_name
            _register_provider(
                repository,
                registry,
                provider,
                display_name=f"Codex {identity_name}",
            )
    return registry


def create_app(
    repository: CareerOpsRepository | None = None,
    *,
    provider_registry: Mapping[str, AiProvider] | None = None,
    allowed_origins: list[str] | None = None,
    auth: BasicAuthConfig | None = None,
    inherit_environment_auth: bool = True,
    static_dir: Path | None = None,
    task_poll_interval_seconds: float = 1.0,
    task_worker_count: int = 2,
) -> FastAPI:
    repo = repository or default_repository()
    registry = (
        dict(provider_registry)
        if provider_registry is not None
        else _default_provider_registry(repo)
    )
    orchestrator = AiOrchestrator(repo, registry)
    task_runner = AiTaskRunner(
        repo,
        orchestrator,
        poll_interval_seconds=task_poll_interval_seconds,
        worker_count=task_worker_count,
    )

    @asynccontextmanager
    async def lifespan(_api: FastAPI):
        runner_started = False
        try:
            try:
                runner_started = task_runner.start()
            except Exception as exc:
                raise RuntimeError("AI task runner failed to start") from exc
            if not runner_started:
                raise RuntimeError("AI task runner failed to start")
            _api.state.task_runner_lifespan_started = True
            yield
        finally:
            _api.state.task_runner_lifespan_started = False
            task_runner.stop()

    root = Path(__file__).resolve().parents[3]
    api = FastAPI(title="CareerOps Local API", lifespan=lifespan)
    api.state.repository = repo
    api.state.provider_registry = registry
    api.state.ai_orchestrator = orchestrator
    api.state.task_runner = task_runner
    api.state.task_runner_lifespan_started = False
    effective_auth = auth
    if effective_auth is None and inherit_environment_auth:
        effective_auth = _auth_from_env()
    _add_basic_auth_middleware(api, effective_auth)
    api.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins or _allowed_origins_from_env(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @api.get("/health")
    def health() -> dict[str, str]:
        try:
            with repo.connection() as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Database unavailable") from exc
        if api.state.task_runner_lifespan_started and not task_runner.is_running:
            raise HTTPException(
                status_code=503, detail="AI task runner unavailable"
            )
        return {"status": "ok", "database": "ok"}

    def submit_ai_task(task_id: str) -> bool:
        return task_runner.submit(
            task_id,
            allow_synchronous=not api.state.task_runner_lifespan_started,
        )

    @api.get("/applications")
    def list_applications() -> list[dict[str, Any]]:
        return [_application_payload(application) for application in repo.list_applications()]

    @api.post("/applications", status_code=201)
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

    @api.get("/applications/{application_id}")
    def get_application(application_id: ApplicationId) -> dict[str, Any]:
        return _application_payload(_get_application(repo, application_id))

    @api.post("/applications/{application_id}/resume", status_code=201)
    async def upload_resume(
        application_id: ApplicationId,
        name: str = Form(...),
        role_family: str = Form("general"),
        tex_file: UploadFile = File(...),
    ) -> dict[str, Any]:
        application = _get_application(repo, application_id)
        if (
            application.approval_id is not None
            or application.status.value == "approved"
            or application.submission_state.value != "not_submitted"
            or application.submitted_at is not None
        ):
            raise HTTPException(status_code=409, detail="Application cannot be changed")
        safe_name = _bounded_form_value(name, "name", 300)
        safe_role_family = _bounded_form_value(role_family, "role_family", 300)
        upload_dir = repo.vault_path / "resumes" / f"upload_{uuid4().hex}"
        committed = False
        try:
            _require_owned_directory(repo.vault_path, repo.vault_path / "resumes")
            upload_dir.mkdir()
            tex_path = upload_dir / "resume.tex"
            content = bytearray()
            while True:
                chunk = await tex_file.read(64 * 1024)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_RESUME_TEX_BYTES:
                    raise HTTPException(status_code=413, detail="Resume source is too large")
            try:
                decoded = bytes(content).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(
                    status_code=422, detail="Resume source must be valid UTF-8"
                ) from exc
            if not decoded.strip() or "\x00" in decoded:
                raise HTTPException(status_code=422, detail="Resume source is invalid")
            with tex_path.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            path_stat = tex_path.lstat()
            if (
                stat.S_ISLNK(path_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or path_stat.st_nlink != 1
            ):
                raise HTTPException(status_code=409, detail="Resume storage is invalid")
            resume = repo.create_and_select_resume_version(
                application_id=application_id,
                name=safe_name,
                role_family=safe_role_family,
                tex_path=tex_path,
                tex_sha256=hashlib.sha256(content).hexdigest(),
            )
            committed = True
            return _resume_payload(resume)
        except HTTPException:
            if not committed:
                _cleanup_upload_directory(repo, upload_dir)
            raise
        except (KeyError, ValueError) as exc:
            if not committed:
                _cleanup_upload_directory(repo, upload_dir)
            raise HTTPException(status_code=409, detail="Resume upload was rejected") from exc
        except Exception as exc:
            if not committed:
                _cleanup_upload_directory(repo, upload_dir)
            raise HTTPException(status_code=500, detail="Resume upload failed") from exc

    @api.get("/resumes/{resume_id}")
    def get_resume(resume_id: ResumeId) -> dict[str, Any]:
        try:
            return _resume_payload(repo.get_resume_version(resume_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resume not found") from exc

    @api.post("/resumes/{resume_id}/render")
    def render_resume(resume_id: ResumeId) -> dict[str, Any]:
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

    @api.post("/applications/{application_id}/resume-drafts", status_code=201)
    def create_resume_draft(
        application_id: ApplicationId,
        payload: CreateResumeDraftRequest,
    ) -> dict[str, Any]:
        try:
            resume = create_verified_resume_draft(
                repo,
                application_id=application_id,
                task_id=payload.task_id,
                finding_ids=payload.finding_ids,
                name=payload.name,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Resume draft authority not found"
            ) from exc
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            raise HTTPException(
                status_code=409, detail="Resume draft could not be created"
            ) from exc
        return _resume_payload(resume)

    @api.post("/applications/{application_id}/analyze")
    def analyze_application(
        application_id: ApplicationId, payload: AnalyzeRequest
    ) -> dict[str, Any]:
        application = _get_application(repo, application_id)
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
            claims=[claim.model_dump(mode="json") for claim in payload.claims],
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

    @api.post("/applications/{application_id}/approve")
    def approve_application(
        application_id: ApplicationId, payload: ApproveRequest
    ) -> dict[str, Any]:
        try:
            approval = repo.approve_application(application_id, payload.report_id, payload.note)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Approval packet not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Application cannot be approved") from exc
        return _approval_payload(approval)

    @api.post("/applications/{application_id}/mark-submitted")
    def mark_submitted(
        application_id: ApplicationId, payload: MarkSubmittedRequest
    ) -> dict[str, Any]:
        try:
            application = repo.mark_submitted_manual(application_id, payload.note)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Application not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Application cannot be submitted") from exc
        return _application_payload(application)

    @api.get("/ai/providers")
    def list_ai_providers() -> list[dict[str, Any]]:
        return [
            _provider_payload(config, registry.get(config.id))
            for config in repo.list_ai_provider_configs()
        ]

    @api.post("/applications/{application_id}/ai-tasks", status_code=202)
    def create_ai_task(
        application_id: ApplicationId,
        payload: CreateAiTaskRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        _get_application(repo, application_id)
        try:
            repo.get_ai_provider_config(payload.provider_config_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="AI provider not found") from exc
        try:
            task = orchestrator.enqueue(
                application_id,
                payload.task_type.value,
                payload.provider_config_id,
                payload.input_artifact_ids,
            )
        except ValueError as exc:
            if "artifact" in str(exc).lower():
                raise HTTPException(status_code=400, detail="Invalid input artifact IDs") from exc
            raise HTTPException(status_code=409, detail="AI task cannot be launched") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="AI task launch failed") from exc
        background_tasks.add_task(submit_ai_task, task.id)
        return _ai_task_payload(task)

    @api.get("/ai-tasks/{task_id}")
    def get_ai_task(task_id: TaskId) -> dict[str, Any]:
        try:
            return _ai_task_payload(repo.get_ai_task(task_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="AI task not found") from exc

    @api.get("/ai-tasks/{task_id}/findings")
    def get_ai_task_findings(task_id: TaskId) -> list[dict[str, Any]]:
        try:
            repo.get_ai_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="AI task not found") from exc
        return [_ai_finding_payload(item) for item in repo.list_ai_findings(task_id)]

    @api.get("/ai-tasks/{task_id}/artifacts")
    def get_ai_task_artifacts(task_id: TaskId) -> list[dict[str, Any]]:
        try:
            repo.get_ai_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="AI task not found") from exc
        try:
            artifacts = repo.list_ai_artifacts(task_id)
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail="AI task artifact integrity check failed",
            ) from exc
        return [_ai_artifact_payload(item) for item in artifacts]

    @api.post("/ai-findings/{finding_id}/decision")
    def decide_ai_finding(
        finding_id: FindingId, payload: FindingDecisionRequest
    ) -> dict[str, Any]:
        try:
            finding = repo.update_ai_finding_decision(
                finding_id,
                payload.decision,
                expected_decision="pending",
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="AI finding not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="AI finding decision conflict") from exc
        return _ai_finding_payload(finding)

    @api.get("/applications/{application_id}/packet")
    def get_application_packet(application_id: ApplicationId) -> dict[str, Any]:
        try:
            snapshot = repo.get_application_packet_snapshot(application_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Application not found") from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="Application packet is invalid") from exc
        return _packet_payload(snapshot)

    @api.get("/applications/{application_id}/requirements")
    def get_requirements(application_id: ApplicationId) -> dict[str, Any]:
        application = _get_application(repo, application_id)
        snapshot = _get_owned_snapshot(repo, application)
        return _requirements_payload(snapshot.extracted_requirements_json)

    @api.put("/applications/{application_id}/requirements")
    def update_requirements(
        application_id: ApplicationId, payload: RequirementsUpdateRequest
    ) -> dict[str, Any]:
        try:
            snapshot = repo.update_user_corrected_requirements(
                application_id, payload.user_corrected
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Application not found") from exc
        except PersistedJsonSizeError as exc:
            raise HTTPException(
                status_code=422, detail="Requirements payload is too large"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Requirements cannot be changed") from exc
        return _requirements_payload(snapshot.extracted_requirements_json)

    @api.get("/profile-facts")
    def list_profile_facts() -> list[dict[str, Any]]:
        return [_profile_fact_payload(item) for item in repo.list_profile_facts()]

    @api.post("/profile-facts", status_code=201)
    def create_profile_fact(payload: ProfileFactRequest) -> dict[str, Any]:
        try:
            fact = repo.create_profile_fact(
                fact_key=payload.fact_key,
                fact_value=payload.fact_value,
                verification_state=payload.verification_state.value,
                privacy_level=payload.privacy_level.value,
                notes=payload.notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Profile fact is invalid") from exc
        return _profile_fact_payload(fact)

    @api.post("/applications/{application_id}/evidence")
    def create_or_link_evidence(
        application_id: ApplicationId, payload: EvidenceRequest
    ) -> Response:
        _get_application(repo, application_id)
        if payload.evidence_id is not None:
            try:
                evidence = repo.get_evidence_item(payload.evidence_id)
                repo.link_application_evidence(application_id, evidence.id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Evidence not found") from exc
            return _json_response(_evidence_payload(evidence), status_code=200)
        try:
            evidence = repo.create_and_link_evidence_item(
                application_id=application_id,
                claim_text=payload.claim_text or "",
                evidence_type=payload.evidence_type or "",
                source_url=payload.source_url or None,
                source_sha256=payload.source_sha256,
                verification_state=payload.verification_state.value,
                privacy_level=payload.privacy_level.value,
                notes=payload.notes or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Evidence is invalid") from exc
        return _json_response(_evidence_payload(evidence), status_code=201)

    @api.get("/applications/{application_id}/evidence")
    def list_application_evidence(application_id: ApplicationId) -> list[dict[str, Any]]:
        _get_application(repo, application_id)
        return [
            _evidence_payload(item)
            for item in repo.list_application_evidence(application_id)
        ]

    @api.get("/applications/{application_id}/versions")
    def list_application_versions(
        application_id: ApplicationId,
    ) -> list[dict[str, Any]]:
        _get_application(repo, application_id)
        return [
            _application_version_payload(item)
            for item in repo.list_application_versions(application_id)
        ]

    @api.get("/applications/{application_id}/artifacts/jd")
    def download_jd(application_id: ApplicationId) -> Response:
        application = _get_application(repo, application_id)
        snapshot = _get_owned_snapshot(repo, application)
        exact_relative = ("applications", application.id, "jd.txt")
        content = _artifact_bytes(
            repo,
            snapshot.raw_text_path,
            snapshot.raw_text_sha256,
            exact_relative=exact_relative,
            max_bytes=MAX_JD_DOWNLOAD_BYTES,
        )
        return _download_response(
            content,
            "text/plain; charset=utf-8",
            _artifact_filename(application, "jd", "txt"),
        )

    @api.get("/applications/{application_id}/artifacts/resume-pdf")
    def download_resume_pdf(application_id: ApplicationId) -> Response:
        application = _get_application(repo, application_id)
        if application.selected_resume_version_id is None:
            raise HTTPException(status_code=404, detail="Selected resume PDF not found")
        try:
            resume = repo.get_resume_version(application.selected_resume_version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Selected resume not found") from exc
        if resume.pdf_path is None or resume.pdf_sha256 is None:
            raise HTTPException(status_code=404, detail="Selected resume PDF not found")
        content = _artifact_bytes(
            repo,
            resume.pdf_path,
            resume.pdf_sha256,
            required_prefix=("resumes",),
            max_bytes=MAX_RESUME_PDF_DOWNLOAD_BYTES,
        )
        return _download_response(
            content,
            "application/pdf",
            _artifact_filename(application, "resume", "pdf"),
        )

    @api.get("/applications/{application_id}/artifacts/report")
    def download_report(application_id: ApplicationId) -> Response:
        application = _get_application(repo, application_id)
        if application.latest_report_id is None:
            raise HTTPException(status_code=404, detail="Current report not found")
        try:
            report = repo.get_report(application.latest_report_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Current report not found") from exc
        if (
            report.application_id != application.id
            or report.resume_version_id != application.selected_resume_version_id
            or report.jd_snapshot_id != application.jd_snapshot_id
        ):
            raise HTTPException(status_code=409, detail="Current report is stale")
        return _json_download(
            _report_payload(report), _artifact_filename(application, "report", "json")
        )

    @api.get("/applications/{application_id}/artifacts/approval")
    def download_approval(application_id: ApplicationId) -> Response:
        application = _get_application(repo, application_id)
        if application.approval_id is None:
            raise HTTPException(status_code=404, detail="Current approval not found")
        try:
            approval = repo.get_approval(application.approval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Current approval not found") from exc
        if (
            approval.id != application.approval_id
            or approval.application_id != application.id
            or approval.resume_version_id != application.selected_resume_version_id
            or approval.report_id != application.latest_report_id
        ):
            raise HTTPException(status_code=409, detail="Current approval is stale")
        try:
            report = repo.get_report(approval.report_id)
            resume = repo.get_resume_version(approval.resume_version_id)
        except KeyError as exc:
            raise HTTPException(status_code=409, detail="Current approval is stale") from exc
        if (
            report.id != application.latest_report_id
            or report.application_id != application.id
            or report.resume_version_id != resume.id
            or report.jd_snapshot_id != application.jd_snapshot_id
            or resume.id != application.selected_resume_version_id
            or resume.pdf_path is None
            or resume.pdf_sha256 is None
            or approval.pdf_sha256 != resume.pdf_sha256
        ):
            raise HTTPException(status_code=409, detail="Current approval is stale")
        try:
            _artifact_bytes(
                repo,
                resume.pdf_path,
                resume.pdf_sha256,
                required_prefix=("resumes",),
                max_bytes=MAX_RESUME_PDF_DOWNLOAD_BYTES,
            )
        except HTTPException as exc:
            raise HTTPException(status_code=409, detail="Current approval is stale") from exc
        return _json_download(
            _approval_payload(approval),
            _artifact_filename(application, "approval", "json"),
        )

    _mount_static_app(api, static_dir if static_dir is not None else root / "apps" / "web" / "dist")
    return api


def _get_application(repo: CareerOpsRepository, application_id: str) -> Application:
    try:
        return repo.get_application(application_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Application not found") from exc


def _get_owned_snapshot(
    repo: CareerOpsRepository, application: Application
) -> JobDescriptionSnapshot:
    try:
        snapshot = repo.get_jd_snapshot(application.jd_snapshot_id)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail="Application job description is missing") from exc
    if snapshot.application_id != application.id:
        raise HTTPException(status_code=409, detail="Application job description is invalid")
    return snapshot


def _packet_payload(snapshot: ApplicationPacketSnapshot) -> dict[str, Any]:
    return {
        "application": _application_payload(snapshot.application),
        "job_description": _jd_payload(snapshot.job_description),
        "selected_resume": (
            _resume_payload(snapshot.selected_resume)
            if snapshot.selected_resume is not None
            else None
        ),
        "latest_report": (
            _report_payload(snapshot.latest_report)
            if snapshot.latest_report is not None
            else None
        ),
        "approval": (
            _approval_payload(snapshot.approval)
            if snapshot.approval is not None
            else None
        ),
        "latest_ai_task": (
            _ai_task_payload(snapshot.latest_ai_task)
            if snapshot.latest_ai_task is not None
            else None
        ),
        "evidence": [_evidence_payload(item) for item in snapshot.evidence],
        "versions": [
            _application_version_payload(item) for item in snapshot.versions
        ],
    }


def _requirements_payload(value: Any) -> dict[str, Any]:
    public_value = _sanitize_public_json(value)
    if not isinstance(public_value, dict):
        raise HTTPException(status_code=409, detail="Stored requirements are invalid")
    grouped = any(
        key in public_value
        for key in ("raw_ai", "deterministic", "user_corrected", "uncertainty")
    )
    result = {
        "raw_ai": public_value.get("raw_ai", {}) if grouped else {},
        "deterministic": (
            public_value.get("deterministic", {}) if grouped else public_value
        ),
        "user_corrected": public_value.get("user_corrected", {}) if grouped else {},
        "uncertainty": public_value.get("uncertainty", {}) if grouped else {},
    }
    if not all(isinstance(group, dict) for group in result.values()):
        raise HTTPException(status_code=409, detail="Stored requirement groups are invalid")
    return result


_PUBLIC_PROVIDER_BINDING_FIELDS = frozenset(
    {
        "provider_kind",
        "model",
        "privacy_mode",
        "data_leaves_machine",
        "is_ai",
        "endpoint",
        "identity_name",
        "executable_sha256",
        "executable_version",
        "reasoning_effort",
        "model_digest",
        "model_metadata_sha256",
        "show_metadata_sha256",
        "attestation_version",
        "egress_policy",
        "cloud_disabled",
        "egress_enforced",
        "isolation_evidence_sha256",
    }
)
def _sanitize_public_json(value: Any, *, provider_binding: bool = False) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, inner in value.items():
            if not isinstance(key, str):
                continue
            normalized_key = key.casefold()
            if provider_binding and normalized_key not in _PUBLIC_PROVIDER_BINDING_FIELDS:
                continue
            sanitized[key] = _sanitize_public_json(
                inner,
                provider_binding=(
                    provider_binding
                    or normalized_key
                    in {"provider_binding", "availability_binding"}
                ),
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_public_json(item, provider_binding=provider_binding)
            for item in value
        ]
    return value


def _provider_payload(
    config: AiProviderConfig, provider: AiProvider | None
) -> dict[str, Any]:
    current: ProviderAvailability | None = None
    if provider is not None:
        try:
            current = provider.availability()
        except Exception:
            current = None
    if not config.enabled:
        available = False
        detail = "disabled"
    elif provider is None:
        available = False
        detail = "runtime_not_registered"
    elif current is None:
        available = False
        detail = "runtime_unavailable"
    elif not AiOrchestrator._availability_matches(config, current):
        available = False
        detail = current.detail if not current.available else "metadata_mismatch"
    else:
        available = True
        detail = current.detail
    current_metadata = _public_provider_metadata(current) if current is not None else None
    persisted_is_ai = bool(config.availability_binding.get("is_ai", False))
    persisted_egress = bool(config.availability_binding.get("data_leaves_machine", False))
    payload = {
        "id": config.id,
        "provider_kind": config.provider_kind,
        "display_name": config.display_name,
        "identity_name": config.identity_name,
        "enabled": config.enabled,
        "privacy_mode": config.privacy_mode.value,
        "endpoint": config.endpoint,
        "model": config.model,
        "available": available,
        "availability_detail": detail,
        "is_ai": current.is_ai if current is not None else persisted_is_ai,
        "data_leaves_machine": (
            current.data_leaves_machine if current is not None else persisted_egress
        ),
        "current_metadata": current_metadata,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }
    safe_payload = redact(payload)
    if not isinstance(safe_payload, dict):
        raise RuntimeError("Provider payload redaction failed")
    return safe_payload


def _public_provider_metadata(status: ProviderAvailability) -> dict[str, Any]:
    metadata = _sanitize_public_json(
        status.model_dump(mode="json"), provider_binding=True
    )
    if not isinstance(metadata, dict):
        raise RuntimeError("Provider metadata sanitization failed")
    metadata["detail"] = status.detail
    return metadata


def _artifact_bytes(
    repo: CareerOpsRepository,
    path: Path,
    expected_sha256: str,
    *,
    max_bytes: int,
    exact_relative: tuple[str, ...] | None = None,
    required_prefix: tuple[str, ...] | None = None,
) -> bytes:
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "") is None:
        raise HTTPException(status_code=409, detail="Artifact checksum is invalid")
    try:
        return _read_owned_regular_file(
            repo.vault_path,
            path,
            expected_sha256,
            max_bytes=max_bytes,
            exact_relative=exact_relative,
            required_prefix=required_prefix,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact file not found") from exc
    except _ArtifactPathError as exc:
        raise HTTPException(status_code=400, detail="Artifact path is invalid") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="Artifact integrity check failed") from exc


def _read_owned_regular_file(
    vault_path: Path,
    path: Path,
    expected_sha256: str,
    *,
    max_bytes: int,
    exact_relative: tuple[str, ...] | None,
    required_prefix: tuple[str, ...] | None,
) -> bytes:
    vault = Path(os.path.abspath(vault_path))
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(vault)
    except ValueError as exc:
        raise _ArtifactPathError("Artifact is outside the vault") from exc
    if exact_relative is not None and relative.parts != exact_relative:
        raise _ArtifactPathError("Artifact does not match its repository location")
    if required_prefix is not None and relative.parts[: len(required_prefix)] != required_prefix:
        raise _ArtifactPathError("Artifact does not match its repository area")
    try:
        root_stat = vault.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise _ArtifactPathError("Vault is not a regular directory")
    current = vault
    path_stat = root_stat
    for part in relative.parts:
        current = current / part
        try:
            path_stat = current.lstat()
        except FileNotFoundError:
            raise
        if stat.S_ISLNK(path_stat.st_mode):
            raise _ArtifactPathError("Artifact path contains a symlink")
    if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1:
        raise _ArtifactPathError("Artifact is not a single-link regular file")
    if path_stat.st_size > max_bytes:
        raise ValueError("Artifact exceeds its size limit")
    canonical = Path(os.path.realpath(candidate))
    try:
        canonical.relative_to(Path(os.path.realpath(vault)))
    except ValueError as exc:
        raise _ArtifactPathError("Artifact resolves outside the vault") from exc
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(canonical, flags)
    try:
        opened = os.fstat(descriptor)
        before_identity = _stat_identity(path_stat)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _stat_identity(opened) != before_identity
        ):
            raise ValueError("Artifact identity changed before read")
        content = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ValueError("Artifact exceeds its size limit")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = canonical.lstat()
    except FileNotFoundError as exc:
        raise ValueError("Artifact identity changed after read") from exc
    if _stat_identity(after) != before_identity or _stat_identity(after_path) != before_identity:
        raise ValueError("Artifact identity changed during read")
    raw = bytes(content)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Artifact checksum does not match")
    return raw


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_nlink,
    )


def _require_owned_directory(vault_path: Path, directory: Path) -> None:
    vault = Path(os.path.abspath(vault_path))
    candidate = Path(os.path.abspath(directory))
    try:
        candidate.relative_to(vault)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Repository storage is invalid") from exc
    for current in (vault, candidate):
        try:
            current_stat = current.lstat()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail="Repository storage is missing") from exc
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise HTTPException(status_code=409, detail="Repository storage is invalid")


def _cleanup_upload_directory(repo: CareerOpsRepository, upload_dir: Path) -> None:
    resumes_root = Path(os.path.abspath(repo.vault_path / "resumes"))
    candidate = Path(os.path.abspath(upload_dir))
    try:
        relative = candidate.relative_to(resumes_root)
    except ValueError:
        return
    if len(relative.parts) != 1 or not relative.name.startswith("upload_"):
        return
    if candidate.is_symlink():
        return
    shutil.rmtree(candidate, ignore_errors=True)


def _bounded_form_value(value: str, label: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or "\x00" in normalized:
        raise HTTPException(status_code=422, detail=f"Invalid {label}")
    return normalized


def _download_response(content: bytes, media_type: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _json_response(payload: Any, *, status_code: int = 200) -> Response:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return Response(content=content, status_code=status_code, media_type="application/json")


def _json_download(payload: Any, filename: str) -> Response:
    response = _json_response(payload)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _artifact_filename(application: Application, kind: str, extension: str) -> str:
    stem = f"{application.company}-{application.role_title}-{kind}".encode(
        "ascii", "ignore"
    ).decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-_")
    stem = stem[:100].rstrip(".-_") or f"careerops-{kind}"
    return f"{stem}.{extension}"


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


def _add_basic_auth_middleware(api: FastAPI, config: BasicAuthConfig | None) -> None:
    if config is None:
        return

    @api.middleware("http")
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
    return secrets.compare_digest(username, config.username) and secrets.compare_digest(
        password, config.password
    )


def _mount_static_app(api: FastAPI, static_dir: Path) -> None:
    static_dir = Path(static_dir)
    index_path = static_dir / "index.html"
    if not index_path.exists():
        return
    assets_dir = static_dir / "assets"
    if assets_dir.exists():
        api.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @api.get("/{full_path:path}", include_in_schema=False)
    def serve_react_app(full_path: str):
        candidate = (static_dir / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(static_dir.resolve()):
            return FileResponse(candidate)
        return FileResponse(index_path)


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
        "role_type": application.role_type,
        "submitted_at": application.submitted_at,
        "created_at": application.created_at,
        "updated_at": application.updated_at,
    }


def _jd_payload(snapshot: JobDescriptionSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "application_id": snapshot.application_id,
        "source_url": snapshot.source_url,
        "raw_text_sha256": snapshot.raw_text_sha256,
        "captured_at": snapshot.captured_at,
        "parser_version": snapshot.parser_version,
        "requirements": _requirements_payload(snapshot.extracted_requirements_json),
    }


def _resume_payload(resume: ResumeVersion) -> dict[str, Any]:
    return {
        "id": resume.id,
        "name": resume.name,
        "role_family": resume.role_family,
        "tex_sha256": resume.tex_sha256,
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
        "fit": _sanitize_public_json(report.fit),
        "risks": _sanitize_public_json(report.risks),
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


def _ai_task_payload(task: AiTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "application_id": task.application_id,
        "task_type": task.task_type.value,
        "provider_config_id": task.provider_config_id,
        "prompt_template_id": task.prompt_template_id,
        "prompt_sha256": task.prompt_sha256,
        "input_artifact_ids": task.input_artifact_ids,
        "status": task.status.value,
        "privacy_mode": task.privacy_mode.value,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "error_summary": task.error_summary,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _ai_finding_payload(finding: AiFinding) -> dict[str, Any]:
    return {
        "id": finding.id,
        "task_id": finding.task_id,
        "finding_type": finding.finding_type,
        "severity": finding.severity.value,
        "claim_text": finding.claim_text,
        "evidence_item_id": finding.evidence_item_id,
        "recommendation": finding.recommendation,
        "verifier_state": finding.verifier_state.value,
        "decision": finding.decision.value,
        "payload": finding.payload,
        "created_at": finding.created_at,
        "updated_at": finding.updated_at,
    }


def _ai_artifact_payload(artifact: AiArtifact) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "task_id": artifact.task_id,
        "artifact_type": artifact.artifact_type,
        "sha256": artifact.sha256,
        "schema_version": artifact.schema_version,
        "created_at": artifact.created_at,
    }


def _profile_fact_payload(fact: ProfileFact) -> dict[str, Any]:
    return {
        "id": fact.id,
        "fact_key": fact.fact_key,
        "fact_value": fact.fact_value,
        "verification_state": fact.verification_state.value,
        "privacy_level": fact.privacy_level.value,
        "notes": fact.notes,
        "created_at": fact.created_at,
        "updated_at": fact.updated_at,
    }


def _evidence_payload(evidence: EvidenceItem) -> dict[str, Any]:
    publishable = (
        evidence.privacy_level.value == "public"
        and evidence.verification_state.value == "verified"
    )
    return {
        "id": evidence.id,
        "claim_text": evidence.claim_text,
        "evidence_type": evidence.evidence_type,
        "source_url": evidence.source_url,
        "source_sha256": evidence.source_sha256,
        "verification_state": evidence.verification_state.value,
        "privacy_level": evidence.privacy_level.value,
        "publishable": publishable,
        "notes": evidence.notes,
        "created_at": evidence.created_at,
        "updated_at": evidence.updated_at,
    }


def _application_version_payload(version: ApplicationVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "application_id": version.application_id,
        "version_number": version.version_number,
        "event_type": version.event_type,
        "snapshot": _sanitize_public_json(version.snapshot),
        "created_at": version.created_at,
    }


app = create_app()
