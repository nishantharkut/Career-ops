# CareerOps PRD

Status: approved direction with AI-first correction pending review.

Source of truth: `docs/context-reconstruction.md` and `docs/superpowers/specs/2026-07-21-ai-first-careerops-design.md`.

Product slice: Application Desk v0.

## 1. Objective

CareerOps v0 is a private AI-assisted application desk for one user: Nishant Harkut. It runs locally by default and can be hosted as a low-cost authenticated single-user app. It must help turn a job description into a safe application packet without inventing facts, leaking private information, or submitting anything without approval.

The first usable workflow must support this loop:

1. Add a job description or application link.
2. Create an application record.
3. Use AI to understand the role against the verified profile and evidence vault.
4. Select or create a LaTeX resume version.
5. Render the PDF locally.
6. Produce an AI-assisted fit report and deterministic risk report.
7. Require user approval before the application is marked ready.

The product is not a chat memory. It is a controlled local AI system with explicit records, evidence, versions, provider metadata, and gates.

## 2. Why This Slice Comes First

The user wants a complete platform later, but the foundation must be reliable before adding discovery or cloud automation.

Application Desk v0 is the correct first slice because it touches the highest-risk parts of the workflow:

- profile facts
- resume claims
- job requirements
- application-specific tailoring
- PDF freshness
- approval state

If these are unreliable, job scraping only creates more bad tasks faster.

## 3. User

Primary user: Nishant Harkut.

Context from reconstruction:

- India-focused internship search.
- Pre-final year constraints.
- Targeting major companies and high-quality internship calls.
- Uses LaTeX resumes.
- Wants reliable, direct, non-bloated guidance.
- Wants repository inspection before accepting metrics.
- Wants AI-based resume and application help, preferably local/open-source for private processing.
- Requires user approval before submission.

## 4. v0 Scope

Application Desk v0 must include:

- local project setup under `C:\N Drive\Internships + Placements\CareerOps`
- local database for applications, resumes, evidence, reports, and approval records
- file vault for LaTeX sources, PDFs, JD snapshots, and evidence files
- one clean local UI for the application desk
- hostable private deployment with same-origin UI/API serving, auth, health checks, and persistent volumes
- AI task framework with versioned prompts, provider records, artifacts, and verifier output
- local OSS AI provider support when a local model runtime is installed
- optional Codex worker mode for deeper repo/session/evidence review
- import or manual entry of a job description
- application record creation
- profile and evidence record management
- resume version tracking separate from application version tracking
- AI-assisted JD parsing and deterministic requirement extraction fallback
- local resume parsing from rendered PDF
- AI-assisted fit report with explainable deterministic checks
- risk report for unsupported claims, NDA-sensitive wording, stale PDFs, chronology issues, title mismatch, and missing evidence
- LaTeX render command integration through a compiler adapter
- approval gate before ready/submitted state
- audit log for every generated or changed artifact

## 5. Non-Goals For v0

These must not be built first:

- automatic job scraping
- cloud watcher
- browser automation for submissions
- Resume Worded scraping or paid-site automation
- fake proprietary ATS score clone
- multi-user accounts
- public multi-user deployment
- unauthenticated public API exposure
- mobile app
- email sending
- auto-apply
- AI chatbot interface as the primary UI
- deterministic-only resume checking as the final product
- hidden AI calls with no provider, privacy, or provenance record
- cloud AI calls without explicit configuration and approval

The v0 app may prepare a packet. It must not submit it.

## 6. Day-1 Success Criteria

After implementation, the user should be able to use CareerOps for a real application tomorrow.

Minimum successful path:

1. Start the local app.
2. Create one application from a JD.
3. Attach or create one LaTeX resume version.
4. Render a fresh one-page PDF.
5. Run AI review and see a fit report and risk report.
6. See all blocking issues before approval.
7. Approve the packet manually.
8. Export or open the PDF and report.

The system is not ready if any of these fail.

Hosted private mode is ready only if the same workflow runs behind auth with durable `data/` and `vault/` volumes. Ephemeral container storage is not acceptable for real use.

## 7. Functional Requirements

### FR1. Local Workspace

CareerOps must store all private data locally by default. Private profile data, resume text, generated answers, evidence, credentials, and scoring results must not be sent to a cloud AI or hosted storage service unless the user explicitly approves that mode.

Local/private AI is the default target. Cloud AI mode is disabled unless the user configures it and approves the task.

### FR2. Application Ledger

Each application record must include:

- company
- role title
- location
- role type
- source URL when available
- JD snapshot text
- application deadline when known
- status
- selected resume version
- generated report versions
- approval state
- submission state
- notes
- timestamps

Application status values:

- `draft`
- `analyzing`
- `needs_evidence`
- `needs_resume_update`
- `ready_for_review`
- `approved`
- `submitted_manual`
- `archived`

Only the user can move an application to `approved` or `submitted_manual`.

### FR3. Profile And Evidence Vault

The profile must separate:

- confirmed facts
- unconfirmed facts
- rejected claims
- evidence records
- private notes

Every claim that appears in a resume or report must have one of these evidence states:

- `verified`
- `user_stated`
- `needs_verification`
- `rejected`
- `private_do_not_publish`

Metrics must not be treated as verified unless linked to evidence.

### FR4. Resume Version History

Resume versions must be separate from applications.

Each resume version must include:

- source `.tex` path
- rendered PDF path
- target role family
- created timestamp
- parent resume version when derived
- checksum of source
- checksum of PDF
- page count
- render status
- parse status
- linked applications

The app must detect stale PDFs when the LaTeX source changes after render.

### FR5. Application Version History

Each application must preserve:

- original JD snapshot
- extracted requirements
- selected resume version at the time of review
- fit report
- risk report
- approval timestamp
- final submitted artifact references

Changing the resume after approval must invalidate the approval for that application.

### FR6. JD Analysis

The system must use AI plus deterministic fallback to extract:

- role title
- company when possible
- location
- required skills
- preferred skills
- degree or year constraints
- internship duration
- work mode
- core responsibilities
- knockout conditions
- keywords worth matching literally
- terms that should not be over-optimized

Extraction must show uncertainty. Unknown fields stay unknown.

The AI result must be stored with provider, model, prompt version, timestamp, input artifact IDs, and output checksum.

### FR7. Resume Analysis

The system must parse the rendered PDF and check:

- one-page constraint
- text extractability
- section order
- chronology
- contact fields
- project names
- experience titles
- achievements
- skills
- education
- dates
- PDF freshness against source

The system must warn if PDF text extraction differs from the intended LaTeX content.

### FR8. Fit Report

The fit report must be transparent. It should use AI for semantic reasoning and deterministic checks for verification. It may include a local numeric score, but the score must be explained by checks.

Required report sections:

- matched requirements
- missing requirements
- weakly supported requirements
- exact keyword gaps
- semantic matches
- knockout risks
- evidence risks
- resume quality issues
- recommended edits

The report must state that it is a local evaluator and not a Resume Worded or employer ATS score.

AI recommendations must be tied to evidence or marked as suggestions needing verification. A model-generated claim cannot become approved resume content without passing the risk gates.

### FR9. Risk Report

The risk report must block approval for:

- invented metrics
- unsupported paper ownership
- QuickIntell title mismatch
- NDA-sensitive module names
- stale PDF
- wrong chronology
- missing render
- unparseable PDF
- unverified claim used as verified
- secret-like text in artifacts
- unapproved submission state

### FR10. Approval Gate

The user must approve application readiness inside the app. Approval must record:

- application ID
- resume version ID
- report version ID
- exact PDF checksum
- timestamp
- approval note

Any change to the selected resume, JD, or report after approval must reset readiness to `ready_for_review`.

### FR11. UI

The UI must be minimal and operational.

It must avoid:

- complex dashboards
- marketing pages
- decorative heroes
- gradient-heavy surfaces
- card-in-card layouts
- fake browser or device chrome
- AI-sounding copy
- jargon labels

The UI must use plain labels such as:

- `Missing evidence`
- `PDF is stale`
- `Needs approval`
- `Claim not verified`
- `NDA-sensitive wording`
- `Ready to review`

### FR12. Export

The user must be able to open or export:

- selected PDF resume
- application report
- risk report
- JD snapshot
- approval record

### FR13. AI Task Framework

CareerOps must treat AI as a structured subsystem, not as free-form chat.

Each AI task must record:

- task type
- provider
- model or runtime
- privacy mode
- prompt version
- input artifacts
- output artifacts
- status
- errors
- verification result

Required v0 task types:

- `analyze_jd`
- `analyze_fit`
- `suggest_resume_edits`
- `draft_cover_letter`
- `review_claims`

Every AI task must return structured output that can be validated before the UI displays or applies it.

### FR14. Provider Modes

The provider layer must support these modes:

- `local_oss`: local model runtime, with Ollama as the recommended first target and LM Studio or another OpenAI-compatible runtime later.
- `codex_worker`: local Codex worker invoked for deep evidence, repository, or session review.
- `openai_api`: optional cloud provider, disabled by default.
- `deterministic`: rule-based fallback and verifier.

The UI must show which provider produced a result and whether private data left the machine.

## 8. Reliability Requirements

Reliability is more important than speed.

The platform must:

- store records durably in SQLite
- write files atomically where practical
- keep artifact checksums
- maintain audit logs
- recover from interrupted renders
- avoid silent overwrites
- never erase user files without explicit approval
- make warnings visible before approval
- fail closed on submissions and approvals
- redact secret-like values from logs
- treat JDs, resumes, and session files as prompt-injection capable input
- validate every AI output against a schema
- create drafts for AI edits instead of overwriting approved artifacts
- keep deterministic verification authoritative over AI confidence

## 9. Privacy Requirements

Private processing must stay local by default.

Hosted private deployment may run the same local-open-source processing stack inside a user-controlled VM or container. It must use auth, HTTPS, persistent volumes, and the same approval gates. It must not use a free hosted database or ephemeral filesystem as the only source of truth.

If hosted mode uses a remote VM for AI, private data lives on that VM. If cloud AI is enabled, the app must record the exact task and provider that received private data.

The cloud watcher planned for later must not receive:

- profile data
- resumes
- generated answers
- evidence files
- credentials
- reports
- application notes

Cloud watcher output can only be public job metadata and notifications.

## 10. UI Requirements

The v0 UI must feel like a calm workbench.

Required qualities:

- clean
- minimal
- readable
- fast to scan
- dense enough for repeated work
- no fake personality layer
- no motivational copy
- no unnecessary animation

The default layout should be:

- left: application list
- center: selected application and JD
- right: checks, resume status, and approval state

Small screens may stack these sections.

## 11. Testing Requirements

The implementation must include:

- unit tests for data models and state transitions
- parser tests for JD and resume text extraction
- risk-gate tests based on historical failure modes
- LaTeX render tests with PDF freshness checks
- integration test for one complete application packet
- Playwright UI test for the day-1 workflow
- responsive UI checks at 320, 375, 414, 768, and desktop widths
- hostability tests for same-origin serving, auth, configurable CORS, health, and persistent local storage settings
- AI provider config tests
- AI output schema tests
- AI prompt-injection tests
- mock-provider tests for CI
- Codex worker command-construction tests before enabling it
- secret scan on generated artifacts
- backup and restore test for local data

## 12. Launch Definition

Application Desk v0 is complete only when:

- docs are current
- all required tests pass
- the app can process one real JD end-to-end
- the PDF render is fresh and parseable
- the report flags known risk cases
- the UI passes responsive checks
- no private data is sent to cloud services
- AI task provenance is recorded for generated drafts and reports
- deterministic verification blocks unsafe AI output
- hosted private mode has auth and durable storage
- approval cannot be bypassed
