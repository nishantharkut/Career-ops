# CareerOps System Design

Status: approved direction, not implemented.

Source of truth: `docs/context-reconstruction.md` and `docs/prd.md`.

## 1. Architecture Decision

CareerOps should be a local web application with a Python backend, a SQLite database, a local file vault, and a React UI.

Chosen stack:

- Backend: Python with FastAPI.
- Data validation: Pydantic models.
- Database: SQLite.
- Database migrations: Alembic or a simple migration runner for v0.
- Frontend: React with TypeScript and Vite.
- UI style: token-based CSS, minimal operational design, Hallmark-informed audit rules.
- PDF and resume processing: local Python tools.
- LaTeX rendering: local compiler adapter.
- UI testing: Playwright.
- Backend testing: pytest.

This stack is chosen because it is local-first, testable, simple to run, and can also be hosted as a low-cost self-contained container when auth, HTTPS, and persistent volumes are configured.

## 2. High-Level Components

### Local API

Owns:

- application records
- resume records
- evidence records
- reports
- approval state
- file indexing
- audit logs

The API must fail closed. If validation, rendering, parsing, or report generation fails, the application cannot be approved.

### Local Database

SQLite stores structured records. It should not store large PDFs or LaTeX files directly. It stores paths, checksums, metadata, and relationships.

### File Vault

The vault stores artifacts:

- JD snapshots
- LaTeX sources
- rendered PDFs
- reports
- evidence files
- exported packets

Files are addressed by stable IDs and checksums.

### Resume Compiler Adapter

The compiler adapter hides local TeX differences.

It must:

- discover available local LaTeX tools
- compile without remote services
- capture stdout and stderr
- produce a PDF checksum
- report page count
- detect missing PDF output
- mark render failures clearly

It should support common local engines through adapters, such as `latexmk`, `pdflatex`, `xelatex`, or an existing local setup. The implementation should discover the available tool instead of assuming one.

### Resume Parser

The parser extracts text from rendered PDFs and checks whether the PDF is usable by ATS-style systems.

It must check:

- text extraction works
- page count is one unless explicitly allowed
- expected sections appear
- dates and role titles are extractable
- bullet text appears in the PDF text layer

### JD Parser

The parser extracts structured requirements from the job description.

It should start deterministic:

- headings
- required skills
- preferred skills
- responsibilities
- eligibility constraints
- location
- duration
- work mode
- year or graduation constraints

It may add local open-source NLP later, but v0 must remain useful without cloud calls.

### Fit Analyzer

The fit analyzer compares JD requirements to resume and profile evidence.

It should produce:

- exact keyword matches
- missing exact keywords
- semantic matches
- weak matches
- knockout risks
- profile evidence gaps
- resume quality issues

The analyzer must explain each finding. A score without reasons is not acceptable.

### Risk Gate Engine

The risk gate engine blocks approval if a known failure mode appears.

Required gate families:

- evidence gates
- privacy gates
- resume-render gates
- chronology gates
- title gates
- NDA wording gates
- secret gates
- approval gates

### UI

The UI is a workbench. In development it talks to the local API. In hosted production it is served by the FastAPI app and uses same-origin API requests.

Primary screens:

- Application Desk
- Application Detail
- Resume Versions
- Evidence Vault
- Report Review
- Settings

No login is needed for local-only v0 because the app is single-user on the user's machine. Hosted v0 must enable Basic Auth before private data is entered.

## 3. Data Model

### Application

Fields:

- `id`
- `company`
- `role_title`
- `role_family`
- `location`
- `source_url`
- `source_kind`
- `jd_snapshot_id`
- `status`
- `selected_resume_version_id`
- `latest_report_id`
- `approval_id`
- `submission_state`
- `deadline`
- `created_at`
- `updated_at`

### JobDescriptionSnapshot

Fields:

- `id`
- `application_id`
- `source_url`
- `raw_text_path`
- `raw_text_sha256`
- `captured_at`
- `extracted_requirements_json`
- `parser_version`

### ResumeVersion

Fields:

- `id`
- `name`
- `role_family`
- `tex_path`
- `tex_sha256`
- `pdf_path`
- `pdf_sha256`
- `parent_resume_version_id`
- `render_status`
- `parse_status`
- `page_count`
- `created_at`
- `updated_at`

### EvidenceItem

Fields:

- `id`
- `claim_text`
- `evidence_type`
- `source_path`
- `source_url`
- `source_sha256`
- `verification_state`
- `privacy_level`
- `notes`
- `created_at`
- `updated_at`

Verification states:

- `verified`
- `user_stated`
- `needs_verification`
- `rejected`
- `private_do_not_publish`

### Claim

Fields:

- `id`
- `claim_text`
- `claim_kind`
- `project_or_experience`
- `evidence_item_id`
- `verification_state`
- `public_safe`
- `created_at`
- `updated_at`

### Report

Fields:

- `id`
- `application_id`
- `resume_version_id`
- `jd_snapshot_id`
- `fit_summary`
- `score_json`
- `risk_json`
- `recommendations_json`
- `blocking_issue_count`
- `warning_count`
- `created_at`

### Approval

Fields:

- `id`
- `application_id`
- `resume_version_id`
- `report_id`
- `pdf_sha256`
- `approved_at`
- `approval_note`

Approval is invalid if the selected resume, JD snapshot, PDF checksum, or report changes.

### AuditEvent

Fields:

- `id`
- `event_type`
- `entity_type`
- `entity_id`
- `before_json`
- `after_json`
- `created_at`

Audit logs must redact secret-like values.

## 4. File Layout

Recommended layout after implementation:

```text
CareerOps/
  docs/
  apps/
    api/
    web/
  careerops/
    core/
    parsers/
    analyzers/
    renderers/
    gates/
  tests/
  data/
    careerops.db
  vault/
    applications/
    resumes/
    evidence/
    reports/
    exports/
```

`data/` and `vault/` must be treated as private local state.

For hosted v0, `data/` and `vault/` must be mounted as persistent volumes. Ephemeral container storage is not acceptable for application history, resume versions, rendered PDFs, reports, or approvals.

## 5. Data Flow

### Create Application

1. User enters company, role, source URL, and JD text.
2. API writes a JD snapshot.
3. JD parser extracts requirements.
4. Application status becomes `draft`.

### Attach Resume

1. User selects an existing resume version or creates a derived version.
2. API records the resume relationship.
3. Resume compiler renders PDF.
4. Parser extracts text from PDF.
5. PDF checksum and parse status are saved.

### Analyze Application

1. Fit analyzer compares JD requirements with resume text and evidence.
2. Risk gate engine evaluates blocking issues.
3. Report is saved.
4. Application status becomes `ready_for_review` if no blockers exist.
5. Application status becomes `needs_evidence` or `needs_resume_update` if blockers exist.

### Approve

1. User reviews PDF, fit report, and risk report.
2. User clicks approval action.
3. API verifies selected resume, PDF checksum, JD snapshot, and report are current.
4. Approval record is written.
5. Application status becomes `approved`.

### Manual Submission

1. User submits outside the app.
2. User records manual submission.
3. App stores timestamp and notes.

The app never auto-submits in v0.

## 6. Scoring Design

The score is local and explainable. It is not a Resume Worded score and not an employer ATS score.

Suggested score parts:

- requirement coverage
- exact keyword coverage
- semantic skill coverage
- knockout risk
- evidence confidence
- resume parse health
- resume quality checks

The UI must show the parts before the total. If a total score is shown, it should be treated as a summary, not a truth.

## 7. Error Handling

Errors must be factual and actionable.

Examples:

- `PDF render failed. Open the render log and fix the LaTeX error.`
- `PDF is stale. Render again before approval.`
- `Claim has no evidence. Link evidence or remove the claim.`
- `JD parser could not find eligibility rules. Review manually.`

No generic `Something went wrong` messages.

## 8. Security And Privacy

The app must:

- run locally by default
- support same-origin hosted serving with auth
- not send private data to cloud APIs
- redact secret-like values in logs
- keep cloud watcher separate from private processing
- keep approval and submission separate
- avoid storing raw credentials

Public hosting must not expose the API without auth and HTTPS. Free hosted databases or object stores must not become the source of truth unless the user explicitly approves storing private resume artifacts in cloud services.

The historical session contains secret-handling failures. This system must treat secret leakage as a blocker.

## 9. Testing Architecture

Required test layers:

- model tests
- migration tests
- parser tests
- analyzer tests
- gate tests
- renderer tests
- API integration tests
- UI Playwright tests
- visual and responsive checks
- artifact integrity checks

The known historical failure modes must become test fixtures.

## 10. Future Architecture

Later phases may add:

- repository inspection service
- local embeddings
- richer resume tailoring
- cloud watcher for public job discovery
- email alert ingestion
- browser-assisted manual workflows

These must not weaken the local privacy boundary.
