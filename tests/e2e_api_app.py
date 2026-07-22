from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException, Request, Response

from apps.api.careerops_api.main import (
    MAX_RESUME_TEX_BYTES,
    _artifact_bytes,
    create_app,
)
from careerops.ai.contracts import ProviderRequest
from careerops.ai.providers.mock import MockProvider
from careerops.core.repository import CareerOpsRepository


UNSAFE_METRIC = "Improved service throughput by 70+ percent without verified evidence."
UNSUPPORTED_OUTPUT_METRIC = "Improved service throughput by 95+ percent without verified evidence."
RESUME_SOURCE = "Worked on healthcare automation testing and release support with evidence-backed claims only."
RESUME_REPLACEMENT = "Worked on healthcare automation testing, release support, and production software delivery."
CONTROL_HEADER = "X-CareerOps-E2E-Control"
CONTROL_TOKEN = "careerops-e2e-control-v1"
LOCAL_WEB_ORIGIN = "http://127.0.0.1:5173"
RUN_NAME_PATTERN = re.compile(r"careerops-e2e-[0-9]+-[0-9a-f]{32}")


@dataclass(frozen=True)
class E2ePaths:
    temp_root: Path
    run_root: Path
    database_path: Path
    vault_path: Path


def _required_absolute_path(name: str) -> Path:
    raw = os.environ.get(name, "")
    if not raw:
        raise RuntimeError(f"{name} is required for the E2E app")
    raw_path = Path(raw)
    if not raw_path.is_absolute():
        raise RuntimeError(f"{name} must be an absolute E2E path")
    if ".." in raw_path.parts:
        raise RuntimeError(f"{name} must not contain path traversal")
    return Path(os.path.abspath(raw_path))


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        junction_check = getattr(path, "is_junction", None)
        if callable(junction_check) and junction_check():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError("E2E run root could not be inspected") from exc
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_point and attributes & reparse_point)


def _validated_e2e_paths() -> E2ePaths:
    temp_root = Path(tempfile.gettempdir()).resolve()
    raw_root = _required_absolute_path("CAREEROPS_E2E_RUN_ROOT")
    if _is_link_or_junction(raw_root):
        raise RuntimeError(
            "CAREEROPS_E2E_RUN_ROOT must not be a symlink or junction"
        )
    run_root = raw_root.resolve(strict=False)
    if (
        run_root.parent != temp_root
        or RUN_NAME_PATTERN.fullmatch(run_root.name) is None
    ):
        raise RuntimeError(
            "CAREEROPS_E2E_RUN_ROOT must be an isolated OS temp directory"
        )
    if not run_root.exists():
        raise RuntimeError("CAREEROPS_E2E_RUN_ROOT must already exist")
    root_stat = run_root.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError("CAREEROPS_E2E_RUN_ROOT must be a regular directory")

    database_path = _required_absolute_path("CAREEROPS_DB_PATH").resolve(strict=False)
    vault_path = _required_absolute_path("CAREEROPS_VAULT_PATH").resolve(strict=False)
    expected_database = run_root / "careerops.db"
    expected_vault = run_root / "vault"
    if database_path != expected_database or vault_path != expected_vault:
        raise RuntimeError("Database and vault must use their exact E2E location")
    try:
        database_path.relative_to(run_root)
        vault_path.relative_to(run_root)
    except ValueError as exc:
        raise RuntimeError("E2E database and vault must be descendants") from exc
    return E2ePaths(temp_root, run_root, database_path, vault_path)


def _same_e2e_paths(expected: E2ePaths) -> E2ePaths:
    current = _validated_e2e_paths()
    if current != expected:
        raise RuntimeError("E2E environment paths changed after startup")
    return current


def _remove_validated_run_root(paths: E2ePaths) -> None:
    if _is_link_or_junction(paths.run_root):
        raise RuntimeError("Refusing to remove an invalid E2E run root")
    run_root = paths.run_root.resolve(strict=False)
    if (
        run_root != paths.run_root
        or run_root.parent != paths.temp_root
        or RUN_NAME_PATTERN.fullmatch(run_root.name) is None
        or paths.database_path != run_root / "careerops.db"
        or paths.vault_path != run_root / "vault"
    ):
        raise RuntimeError("Refusing to remove an invalid E2E run root")
    if not run_root.exists():
        return
    root_stat = run_root.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError("Refusing to remove a non-directory E2E run root")
    shutil.rmtree(run_root)


def _require_control(request: Request) -> None:
    if request.headers.get(CONTROL_HEADER) != CONTROL_TOKEN:
        raise HTTPException(status_code=404, detail="Not found")


def _mock_output(request: ProviderRequest) -> Mapping[str, Any]:
    context = json.loads(request.context_text())
    linked_evidence = context.get("linked_evidence", [])
    evidence_ids = [
        item["id"]
        for item in linked_evidence
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    evidence_refs = evidence_ids[:1]

    if request.task_type == "analyze_jd":
        return {
            "task_type": "analyze_jd",
            "normalized_title": "Data Science Intern",
            "normalized_company": "Swiggy",
            "normalized_location": "Bangalore",
            "responsibilities": [],
            "required_skills": ["Python", "SQL"],
            "preferred_skills": [],
            "keywords": ["Machine learning"],
            "uncertainties": [],
        }
    if request.task_type == "analyze_fit":
        selected_resume = context.get("selected_resume", {})
        source_text = (
            selected_resume.get("source_text", "")
            if isinstance(selected_resume, dict)
            else ""
        )
        return {
            "task_type": "analyze_fit",
            "score": 90,
            "matches": (
                [UNSUPPORTED_OUTPUT_METRIC] if UNSAFE_METRIC in source_text else []
            ),
            "gaps": [],
            "blockers": [],
            "warnings": [],
            "recommendations": [],
        }
    if request.task_type == "suggest_resume_edits":
        return {
            "task_type": "suggest_resume_edits",
            "suggestions": [
                {
                    "source_text": RESUME_SOURCE,
                    "replacement_text": RESUME_REPLACEMENT,
                    "reason": "Aligns the wording with the evidence",
                    "evidence_refs": evidence_refs,
                }
            ],
        }
    if request.task_type == "draft_cover_letter":
        return {
            "task_type": "draft_cover_letter",
            "plain_text": "I am excited to apply.",
            "evidence_refs": evidence_refs,
        }
    if request.task_type == "review_claims":
        return {
            "task_type": "review_claims",
            "decisions": [
                {
                    "claim_id": claim_id,
                    "decision": "supported",
                    "reason": "Matches the supplied evidence",
                    "evidence_refs": evidence_refs,
                }
                for claim_id in request.expected_claim_ids
            ],
        }
    raise ValueError("Unsupported E2E mock task")


def create_e2e_app() -> FastAPI:
    paths = _validated_e2e_paths()
    repo = CareerOpsRepository(paths.database_path, paths.vault_path)
    repo.initialize()
    provider = MockProvider(_mock_output)
    availability = provider.availability()
    binding = availability.model_dump(mode="json")
    binding.pop("available")
    binding.pop("detail")
    config = repo.upsert_ai_provider_config(
        provider_kind=availability.provider_kind,
        display_name="E2E mock provider",
        privacy_mode=availability.privacy_mode.value,
        endpoint=availability.endpoint,
        identity_name=availability.identity_name,
        model=availability.model,
        enabled=True,
        availability_binding=binding,
    )
    app = create_app(
        repo,
        provider_registry={config.id: provider},
        allowed_origins=[LOCAL_WEB_ORIGIN],
        inherit_environment_auth=False,
        static_dir=paths.run_root / "no-static-content",
    )

    @app.get("/__e2e__/control")
    def control(request: Request) -> dict[str, str]:
        _require_control(request)
        _same_e2e_paths(paths)
        return {"run_name": paths.run_root.name}

    @app.get("/__e2e__/resumes/{resume_id}/source-sha256")
    def resume_source_sha256(resume_id: str, request: Request) -> dict[str, str]:
        _require_control(request)
        try:
            _same_e2e_paths(paths)
            resume = repo.get_resume_version(resume_id)
            source = _artifact_bytes(
                repo,
                resume.tex_path,
                resume.tex_sha256,
                max_bytes=MAX_RESUME_TEX_BYTES,
                required_prefix=("resumes",),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Resume not found") from exc
        except (HTTPException, OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail="Resume source integrity check failed",
            ) from exc
        return {"sha256": hashlib.sha256(source).hexdigest()}

    @app.post("/__e2e__/cleanup", status_code=204)
    def cleanup(request: Request) -> Response:
        _require_control(request)
        try:
            _same_e2e_paths(paths)
            if not app.state.task_runner.stop():
                raise HTTPException(
                    status_code=503,
                    detail="E2E task runner did not quiesce",
                )
            _remove_validated_run_root(paths)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail="E2E cleanup safety check failed",
            ) from exc
        return Response(status_code=204)

    return app
