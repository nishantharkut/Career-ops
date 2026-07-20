# CareerOps System Design

Status: approved direction with AI-first correction pending review.

Source of truth: `docs/context-reconstruction.md`, `docs/prd.md`, and `docs/superpowers/specs/2026-07-21-ai-first-careerops-design.md`.

## 1. Architecture Decision

CareerOps should be a local AI-assisted web application with a Python backend, a SQLite database, a local file vault, provider adapters, deterministic verification, and a React UI.

Chosen stack:

- Backend: Python with FastAPI.
- Data validation: Pydantic models.
- Database: SQLite.
- Database migrations: Alembic or a simple migration runner for v0.
- Frontend: React with TypeScript and Vite.
- UI style: token-based CSS, minimal operational design, Hallmark-informed audit rules.
- AI orchestration: explicit AI task records with provider adapters and structured output schemas.
- Primary AI target: local OSS runtime when installed.
- Deep-review AI target: optional local Codex worker.
- Cloud AI target: optional OpenAI API provider, disabled by default.
- PDF and resume processing: local Python tools.
- LaTeX rendering: local compiler adapter.
- UI testing: Playwright.
- Backend testing: pytest.

This stack is chosen because it is local-first, AI-capable, testable, simple to run, and can also be hosted as a low-cost self-contained container when auth, HTTPS, and persistent volumes are configured.

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
- AI task orchestration
- provider configuration
- deterministic verification of AI output

The API must fail closed. If validation, rendering, parsing, or report generation fails, the application cannot be approved.

### AI Task Orchestrator

The orchestrator owns model-facing work.

It must:

- build bounded context packets from selected artifacts
- choose a configured provider
- call the provider adapter
- validate structured output
- store output as versioned artifacts
- run deterministic verifiers
- expose status to the UI

AI tasks are asynchronous and resumable. Provider failure must leave application, resume, and approval state unchanged.

### Provider Adapter Layer

Provider adapters normalize different AI runtimes into one contract.

Required provider kinds:

- `local_oss`: local model runtime, with Ollama as the recommended first target and LM Studio or another OpenAI-compatible runtime later.
- `codex_worker`: local Codex worker for deep repository, evidence, and session review.
- `openai_api`: optional cloud AI provider, disabled by default.
- `deterministic`: rule-based fallback and verifier.

Provider output must include:

- provider kind
- model or runtime name
- privacy mode
- prompt version
- structured JSON
- raw output artifact path when safe
- error summary when failed

### Deterministic Verifier

The verifier is authoritative over AI confidence.

It checks:

- output schema
- unsupported claims
- evidence mismatch
- stale PDF
- unsafe title phrasing
- NDA-sensitive wording
- secret-like text
- prompt-injection artifacts
- approval bypass attempts

If the verifier blocks an AI suggestion, the suggestion can be shown as blocked but cannot be accepted into an approved packet.

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

It should be hybrid:

- AI extracts semantic requirements, hidden expectations, role family, and likely ATS terms.
- Deterministic parsing extracts headings, required skills, preferred skills, responsibilities, eligibility constraints, location, duration, work mode, and year constraints.
- The final result preserves uncertainty and records the provider.

The deterministic parser remains a fallback and verifier. It is not the full product.

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

AI should handle semantic reasoning and edit suggestions. Deterministic checks should verify parse health, keyword coverage, evidence status, and blockers. A score without reasons is not acceptable.

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
- AI Tasks

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

### AiProviderConfig

Fields:

- `id`
- `provider_kind`
- `display_name`
- `enabled`
- `privacy_mode`
- `endpoint`
- `model`
- `created_at`
- `updated_at`

Secrets must not be stored in plaintext in SQLite.

### AiTask

Fields:

- `id`
- `application_id`
- `task_type`
- `provider_config_id`
- `input_artifact_ids`
- `output_artifact_ids`
- `status`
- `privacy_mode`
- `started_at`
- `completed_at`
- `error_summary`

### AiArtifact

Fields:

- `id`
- `task_id`
- `artifact_type`
- `path`
- `sha256`
- `schema_version`
- `created_at`

### AiFinding

Fields:

- `id`
- `task_id`
- `finding_type`
- `severity`
- `claim_text`
- `evidence_item_id`
- `recommendation`
- `verifier_state`

### PromptTemplate

Fields:

- `id`
- `name`
- `task_type`
- `version`
- `template_path`
- `sha256`
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
    ai/
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
    ai-tasks/
    exports/
```

`data/` and `vault/` must be treated as private local state.

For hosted v0, `data/` and `vault/` must be mounted as persistent volumes. Ephemeral container storage is not acceptable for application history, resume versions, rendered PDFs, reports, or approvals.

## 5. Data Flow

### Create Application

1. User enters company, role, source URL, and JD text.
2. API writes a JD snapshot.
3. AI task extracts semantic requirements when a provider is enabled.
4. Deterministic parser extracts fallback requirements and verifier facts.
5. Application status becomes `draft`.

### Attach Resume

1. User selects an existing resume version or creates a derived version.
2. API records the resume relationship.
3. Resume compiler renders PDF.
4. Parser extracts text from PDF.
5. PDF checksum and parse status are saved.

### Analyze Application

1. AI task compares JD requirements with resume text, profile facts, and evidence.
2. Fit analyzer computes deterministic exact-match, parse-health, and evidence checks.
3. Risk gate engine evaluates blocking issues.
4. Report and AI artifacts are saved.
5. Application status becomes `ready_for_review` if no blockers exist.
6. Application status becomes `needs_evidence` or `needs_resume_update` if blockers exist.

### Run AI Task

1. User selects a structured task such as `Analyze JD`, `Suggest resume edits`, or `Codex deep review`.
2. API builds a bounded context packet from approved artifacts only.
3. Provider adapter runs the task.
4. Output is validated against schema.
5. Verifier evaluates safety and evidence.
6. UI shows accepted, warning, and blocked suggestions separately.
7. Accepting a suggestion creates a new draft artifact version.

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

The score is local, AI-assisted, and explainable. It is not a Resume Worded score and not an employer ATS score.

Suggested score parts:

- requirement coverage
- exact keyword coverage
- semantic skill coverage
- knockout risk
- evidence confidence
- resume parse health
- resume quality checks
- AI semantic match confidence
- AI edit usefulness after verification

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
- not send private data to cloud APIs unless cloud AI mode is explicitly configured and approved
- redact secret-like values in logs
- keep cloud watcher separate from private processing
- keep approval and submission separate
- avoid storing raw credentials
- treat model prompts and outputs as private artifacts
- prevent prompt injection from JD, resume, or session text

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
- AI provider adapter tests
- AI task schema tests
- prompt-injection tests
- mock-provider CI tests
- Codex worker command-construction tests

The known historical failure modes must become test fixtures.

## 10. Future Architecture

Later phases may add:

- repository inspection service
- local embeddings and vector search
- richer resume tailoring
- cloud watcher for public job discovery
- email alert ingestion
- browser-assisted manual workflows
- optional cloud AI provider

These must not weaken the local privacy boundary.
