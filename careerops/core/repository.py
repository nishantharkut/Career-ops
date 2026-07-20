from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4

from careerops.core.database import connect_database, initialize_database
from careerops.core.models import (
    Application,
    Approval,
    AuditEvent,
    JobDescriptionSnapshot,
    Report,
    ResumeVersion,
)
from careerops.core.status import ApplicationStatus, SubmissionState


SECRET_PATTERNS = (
    re.compile(r"(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [redact(inner) for inner in value]
    if isinstance(value, str):
        redacted = value
        for pattern in SECRET_PATTERNS:
            redacted = pattern.sub(r"\1=[REDACTED]", redacted)
        return redacted
    return value


class CareerOpsRepository:
    def __init__(self, database_path: Path, vault_path: Path):
        self.database_path = Path(database_path)
        self.vault_path = Path(vault_path)

    def initialize(self) -> None:
        initialize_database(self.database_path)
        for directory in ("applications", "resumes", "evidence", "reports", "exports"):
            (self.vault_path / directory).mkdir(parents=True, exist_ok=True)

    def create_application(
        self,
        *,
        company: str,
        role_title: str,
        location: str,
        source_url: str,
        jd_text: str,
        deadline: str | None = None,
        notes: str = "",
    ) -> Application:
        application_id = new_id("app")
        snapshot_id = new_id("jd")
        now = utc_now()
        snapshot_dir = self.vault_path / "applications" / application_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        raw_text_path = snapshot_dir / "jd.txt"
        raw_text_path.write_text(jd_text, encoding="utf-8")
        source_kind = "url" if source_url else "manual"
        jd_sha = sha256_text(jd_text)

        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO jd_snapshots (
                    id, application_id, source_url, raw_text_path, raw_text_sha256,
                    captured_at, extracted_requirements_json, parser_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    application_id,
                    source_url,
                    str(raw_text_path),
                    jd_sha,
                    now,
                    json.dumps({}),
                    "unparsed",
                ),
            )
            connection.execute(
                """
                INSERT INTO applications (
                    id, company, role_title, location, source_url, source_kind,
                    jd_snapshot_id, status, selected_resume_version_id,
                    latest_report_id, approval_id, submission_state, deadline,
                    notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    company,
                    role_title,
                    location,
                    source_url,
                    source_kind,
                    snapshot_id,
                    ApplicationStatus.DRAFT.value,
                    None,
                    None,
                    None,
                    SubmissionState.NOT_SUBMITTED.value,
                    deadline,
                    notes,
                    now,
                    now,
                ),
            )
            self._write_audit(
                connection,
                "application.created",
                "application",
                application_id,
                None,
                {"company": company, "role_title": role_title, "jd_snapshot_id": snapshot_id},
            )
        return self.get_application(application_id)

    def create_resume_version(
        self,
        *,
        name: str,
        role_family: str,
        tex_path: Path,
        tex_sha256: str,
        pdf_path: Path | None = None,
        pdf_sha256: str | None = None,
        parent_resume_version_id: str | None = None,
        render_status: str = "not_rendered",
        parse_status: str = "not_parsed",
        page_count: int | None = None,
    ) -> ResumeVersion:
        resume_id = new_id("res")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO resume_versions (
                    id, name, role_family, tex_path, tex_sha256, pdf_path, pdf_sha256,
                    parent_resume_version_id, render_status, parse_status, page_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resume_id,
                    name,
                    role_family,
                    str(tex_path),
                    tex_sha256,
                    str(pdf_path) if pdf_path else None,
                    pdf_sha256,
                    parent_resume_version_id,
                    render_status,
                    parse_status,
                    page_count,
                    now,
                    now,
                ),
            )
            self._write_audit(
                connection,
                "resume.created",
                "resume_version",
                resume_id,
                None,
                {"name": name, "role_family": role_family, "tex_sha256": tex_sha256},
            )
        return self.get_resume_version(resume_id)

    def update_resume_render(
        self,
        resume_version_id: str,
        *,
        pdf_path: Path | None,
        pdf_sha256: str | None,
        render_status: str,
        parse_status: str,
        page_count: int | None,
    ) -> ResumeVersion:
        resume = self.get_resume_version(resume_version_id)
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE resume_versions
                SET pdf_path = ?, pdf_sha256 = ?, render_status = ?, parse_status = ?,
                    page_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(pdf_path) if pdf_path else None,
                    pdf_sha256,
                    render_status,
                    parse_status,
                    page_count,
                    now,
                    resume_version_id,
                ),
            )
            self._write_audit(
                connection,
                "resume.render_updated",
                "resume_version",
                resume_version_id,
                resume.__dict__,
                {
                    "pdf_path": str(pdf_path) if pdf_path else None,
                    "pdf_sha256": pdf_sha256,
                    "render_status": render_status,
                    "parse_status": parse_status,
                    "page_count": page_count,
                },
            )
        return self.get_resume_version(resume_version_id)

    def select_resume(self, application_id: str, resume_version_id: str) -> Application:
        application = self.get_application(application_id)
        self.get_resume_version(resume_version_id)
        status = (
            ApplicationStatus.NEEDS_RESUME_UPDATE
            if application.approval_id or application.latest_report_id
            else application.status
        )
        now = utc_now()
        before = application.__dict__
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE applications
                SET selected_resume_version_id = ?, latest_report_id = NULL,
                    approval_id = NULL, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (resume_version_id, status.value, now, application_id),
            )
            self._write_audit(
                connection,
                "application.resume_selected",
                "application",
                application_id,
                before,
                {"selected_resume_version_id": resume_version_id, "status": status.value},
            )
        return self.get_application(application_id)

    def save_report(
        self,
        *,
        application_id: str,
        resume_version_id: str,
        jd_snapshot_id: str,
        fit: dict[str, Any],
        risks: dict[str, Any],
    ) -> Report:
        report_id = new_id("rep")
        now = utc_now()
        blockers = risks.get("blockers", [])
        warnings = risks.get("warnings", [])
        status = ApplicationStatus.READY_FOR_REVIEW if not blockers else ApplicationStatus.NEEDS_EVIDENCE
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO reports (
                    id, application_id, resume_version_id, jd_snapshot_id, fit_json,
                    risks_json, blocking_issue_count, warning_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    application_id,
                    resume_version_id,
                    jd_snapshot_id,
                    json.dumps(fit),
                    json.dumps(risks),
                    len(blockers),
                    len(warnings),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE applications
                SET latest_report_id = ?, approval_id = NULL, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (report_id, status.value, now, application_id),
            )
            self._write_audit(
                connection,
                "report.created",
                "report",
                report_id,
                None,
                {
                    "application_id": application_id,
                    "blocking_issue_count": len(blockers),
                    "warning_count": len(warnings),
                },
            )
        return self.get_report(report_id)

    def approve_application(self, application_id: str, report_id: str, note: str) -> Approval:
        application = self.get_application(application_id)
        report = self.get_report(report_id)
        resume = self.get_resume_version(report.resume_version_id)
        if report.application_id != application.id:
            raise ValueError("Report does not belong to application")
        if report.blocking_issue_count:
            raise ValueError("Application has blocking risks")
        if application.selected_resume_version_id != report.resume_version_id:
            raise ValueError("Selected resume differs from report resume")
        if application.jd_snapshot_id != report.jd_snapshot_id:
            raise ValueError("Selected JD differs from report JD")
        if not resume.pdf_sha256:
            raise ValueError("Selected resume has no PDF checksum")

        approval_id = new_id("apr")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    id, application_id, resume_version_id, report_id, pdf_sha256,
                    approved_at, approval_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    application.id,
                    resume.id,
                    report.id,
                    resume.pdf_sha256,
                    now,
                    note,
                ),
            )
            connection.execute(
                """
                UPDATE applications
                SET approval_id = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (approval_id, ApplicationStatus.APPROVED.value, now, application.id),
            )
            self._write_audit(
                connection,
                "application.approved",
                "application",
                application.id,
                application.__dict__,
                {"approval_id": approval_id, "report_id": report.id, "pdf_sha256": resume.pdf_sha256},
            )
        return self.get_approval(approval_id)

    def mark_submitted_manual(self, application_id: str, note: str) -> Application:
        application = self.get_application(application_id)
        if application.status != ApplicationStatus.APPROVED:
            raise ValueError("Application must be approved before manual submission is recorded")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE applications
                SET status = ?, submission_state = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    ApplicationStatus.SUBMITTED_MANUAL.value,
                    SubmissionState.SUBMITTED_MANUAL.value,
                    note,
                    now,
                    application_id,
                ),
            )
            self._write_audit(
                connection,
                "application.submitted_manual",
                "application",
                application_id,
                application.__dict__,
                {"submission_state": SubmissionState.SUBMITTED_MANUAL.value, "notes": note},
            )
        return self.get_application(application_id)

    def get_application(self, application_id: str) -> Application:
        row = self._one("SELECT * FROM applications WHERE id = ?", (application_id,))
        return self._application_from_row(row)

    def list_applications(self) -> list[Application]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()
        return [self._application_from_row(row) for row in rows]

    def get_jd_snapshot(self, snapshot_id: str) -> JobDescriptionSnapshot:
        row = self._one("SELECT * FROM jd_snapshots WHERE id = ?", (snapshot_id,))
        return JobDescriptionSnapshot(
            id=row["id"],
            application_id=row["application_id"],
            source_url=row["source_url"],
            raw_text_path=Path(row["raw_text_path"]),
            raw_text_sha256=row["raw_text_sha256"],
            captured_at=row["captured_at"],
            extracted_requirements_json=json.loads(row["extracted_requirements_json"]),
            parser_version=row["parser_version"],
        )

    def get_resume_version(self, resume_version_id: str) -> ResumeVersion:
        row = self._one("SELECT * FROM resume_versions WHERE id = ?", (resume_version_id,))
        return self._resume_from_row(row)

    def list_resume_versions(self) -> list[ResumeVersion]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM resume_versions ORDER BY created_at DESC").fetchall()
        return [self._resume_from_row(row) for row in rows]

    def get_report(self, report_id: str) -> Report:
        row = self._one("SELECT * FROM reports WHERE id = ?", (report_id,))
        return Report(
            id=row["id"],
            application_id=row["application_id"],
            resume_version_id=row["resume_version_id"],
            jd_snapshot_id=row["jd_snapshot_id"],
            fit=json.loads(row["fit_json"]),
            risks=json.loads(row["risks_json"]),
            blocking_issue_count=row["blocking_issue_count"],
            warning_count=row["warning_count"],
            created_at=row["created_at"],
        )

    def get_approval(self, approval_id: str) -> Approval:
        row = self._one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return Approval(
            id=row["id"],
            application_id=row["application_id"],
            resume_version_id=row["resume_version_id"],
            report_id=row["report_id"],
            pdf_sha256=row["pdf_sha256"],
            approved_at=row["approved_at"],
            approval_note=row["approval_note"],
        )

    def list_audit_events(self) -> list[AuditEvent]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM audit_events ORDER BY created_at ASC, id ASC").fetchall()
        return [
            AuditEvent(
                id=row["id"],
                event_type=row["event_type"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                before_json=json.loads(row["before_json"]),
                after_json=json.loads(row["after_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def connection(self) -> sqlite3.Connection:
        return connect_database(self.database_path)

    def _one(self, query: str, parameters: tuple[Any, ...]) -> sqlite3.Row:
        with self.connection() as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            raise KeyError(f"No row for query: {query}")
        return row

    def _write_audit(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        entity_type: str,
        entity_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                id, event_type, entity_type, entity_id, before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("aud"),
                event_type,
                entity_type,
                entity_id,
                json.dumps(redact(before or {}), default=str),
                json.dumps(redact(after or {}), default=str),
                utc_now(),
            ),
        )

    def _application_from_row(self, row: sqlite3.Row) -> Application:
        return Application(
            id=row["id"],
            company=row["company"],
            role_title=row["role_title"],
            location=row["location"],
            source_url=row["source_url"],
            source_kind=row["source_kind"],
            jd_snapshot_id=row["jd_snapshot_id"],
            status=ApplicationStatus(row["status"]),
            selected_resume_version_id=row["selected_resume_version_id"],
            latest_report_id=row["latest_report_id"],
            approval_id=row["approval_id"],
            submission_state=SubmissionState(row["submission_state"]),
            deadline=row["deadline"],
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _resume_from_row(self, row: sqlite3.Row) -> ResumeVersion:
        return ResumeVersion(
            id=row["id"],
            name=row["name"],
            role_family=row["role_family"],
            tex_path=Path(row["tex_path"]),
            tex_sha256=row["tex_sha256"],
            pdf_path=Path(row["pdf_path"]) if row["pdf_path"] else None,
            pdf_sha256=row["pdf_sha256"],
            parent_resume_version_id=row["parent_resume_version_id"],
            render_status=row["render_status"],
            parse_status=row["parse_status"],
            page_count=row["page_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
