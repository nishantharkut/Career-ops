from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from careerops.ai.context import load_prompt_template
from careerops.core.repository import CareerOpsRepository


def _provider_binding() -> dict[str, Any]:
    return {
        "provider_kind": "mock",
        "model": "approval-fixture-v1",
        "privacy_mode": "local_private",
        "data_leaves_machine": False,
        "is_ai": True,
        "endpoint": "test://approval-ai",
        "identity_name": "",
        "executable": "",
        "executable_sha256": "",
        "executable_version": "",
        "codex_home": "",
        "reasoning_effort": "",
        "model_digest": "",
        "model_metadata_sha256": "",
        "show_metadata_sha256": "",
        "attestation_version": "",
        "runtime_instance_id": "",
        "runtime_handle": "",
        "container_image_reference": "",
        "container_image_id": "",
        "issuer_instance_id": "",
        "egress_policy": "",
        "cloud_disabled": False,
        "egress_enforced": False,
        "isolation_evidence_sha256": "",
        "isolation_evidence_signature": "",
    }


def create_ai_approval_report(
    repo: CareerOpsRepository,
    application_id: str,
    resume_id: str,
):
    application = repo.get_application(application_id)
    resume = repo.get_resume_version(resume_id)
    owned_dir = repo.vault_path / "resumes" / resume.id / "approval-fixture"
    owned_dir.mkdir(parents=True, exist_ok=True)

    try:
        resume.tex_path.resolve().relative_to(repo.vault_path.resolve())
        source_bytes = resume.tex_path.read_bytes()
        source_path = resume.tex_path
    except (FileNotFoundError, OSError):
        source_bytes = b"approval fixture resume source"
        source_path = owned_dir / "resume.tex"
        source_path.write_bytes(source_bytes)
    except ValueError:
        source_bytes = (
            resume.tex_path.read_bytes()
            if resume.tex_path.exists()
            else b"approval fixture resume source"
        )
        source_path = owned_dir / "resume.tex"
        source_path.write_bytes(source_bytes)

    try:
        if resume.pdf_path is None:
            raise FileNotFoundError
        resume.pdf_path.resolve().relative_to(repo.vault_path.resolve())
        pdf_bytes = resume.pdf_path.read_bytes()
        pdf_path = resume.pdf_path
    except (FileNotFoundError, OSError):
        pdf_bytes = b""
        pdf_path = owned_dir / "resume.pdf"
    except ValueError:
        pdf_bytes = (
            resume.pdf_path.read_bytes()
            if resume.pdf_path is not None and resume.pdf_path.exists()
            else b""
        )
        pdf_path = owned_dir / "resume.pdf"
    if pdf_path == owned_dir / "resume.pdf":
        if not pdf_bytes:
            pdf_bytes = b"%PDF-1.4\napproval fixture"
        pdf_path.write_bytes(pdf_bytes)

    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    with repo.connection() as connection:
        connection.execute(
            """
            UPDATE resume_versions
            SET tex_path = ?, tex_sha256 = ?, pdf_path = ?, pdf_sha256 = ?,
                render_status = 'rendered', parse_status = 'parsed',
                page_count = COALESCE(page_count, 1)
            WHERE id = ?
            """,
            (str(source_path), source_sha256, str(pdf_path), pdf_sha256, resume_id),
        )

    binding = _provider_binding()
    provider = repo.upsert_ai_provider(
        provider_kind="mock",
        display_name="Approval test AI",
        privacy_mode="local_private",
        endpoint="test://approval-ai",
        model="approval-fixture-v1",
        enabled=True,
        availability_binding=binding,
    )
    prompt = load_prompt_template("analyze_fit")
    task = repo.create_ai_task(
        application_id=application_id,
        task_type="analyze_fit",
        provider_config_id=provider.id,
        prompt_template_id=prompt.id,
        prompt_sha256=prompt.sha256,
        input_artifact_ids=[],
        privacy_mode="local_private",
        expected_provider_binding=provider.availability_binding,
        expected_provider_binding_sha256=provider.availability_binding_sha256,
    )
    claim = repo.claim_ai_task(task.id)
    assert isinstance(claim, str)
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    attempt_dir = repo.vault_path / "ai-tasks" / task.id / "attempt-fixture"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    context = json.dumps(
        {
            "task_type": "analyze_fit",
            "job_description": {"snapshot_id": application.jd_snapshot_id},
            "selected_resume": {"version_id": resume_id},
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    for artifact_type, content in (
        ("context", context),
        ("raw_output", b'{"task_type":"analyze_fit","score":100}'),
        ("validated_output", b'{"task_type":"analyze_fit","score":100}'),
    ):
        artifact_path = attempt_dir / f"{artifact_type}.json"
        artifact_path.write_bytes(content)
        repo.save_ai_artifact(
            task.id,
            claim,
            artifact_type,
            artifact_path,
            hashlib.sha256(content).hexdigest(),
            "1",
        )

    completed = repo.finalize_ai_task_attempt(
        task_id=task.id,
        claim_token=claim,
        terminal_status="completed",
        error_summary="",
        expected_jd_snapshot_id=application.jd_snapshot_id,
        expected_resume_version_id=resume_id,
        findings=[],
        success_payload={
            "fit": {"score": 100},
            "risks": {"blockers": [], "warnings": []},
        },
    )
    assert completed.status.value == "completed"
    report_id = repo.get_application(application_id).latest_report_id
    assert report_id is not None
    return repo.get_report(report_id)
