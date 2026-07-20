# CareerOps PRD

Status: approved direction, not implemented.

Source of truth: `docs/context-reconstruction.md`.

Product slice: Application Desk v0.

## 1. Objective

CareerOps v0 is a local application desk for one user: Nishant Harkut. It must help turn a job description into a safe application packet without inventing facts, leaking private information, or submitting anything without approval.

The first usable workflow must support this loop:

1. Add a job description or application link.
2. Create an application record.
3. Compare the role against the verified profile and evidence vault.
4. Select or create a LaTeX resume version.
5. Render the PDF locally.
6. Produce a local fit and risk report.
7. Require user approval before the application is marked ready.

The product is not a chat memory. It is a controlled local system with explicit records, evidence, versions, and gates.

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
- Wants local and open-source resume analysis.
- Requires user approval before submission.

## 4. v0 Scope

Application Desk v0 must include:

- local project setup under `C:\N Drive\Internships + Placements\CareerOps`
- local database for applications, resumes, evidence, reports, and approval records
- file vault for LaTeX sources, PDFs, JD snapshots, and evidence files
- one clean local UI for the application desk
- import or manual entry of a job description
- application record creation
- profile and evidence record management
- resume version tracking separate from application version tracking
- local JD parsing and requirement extraction
- local resume parsing from rendered PDF
- local fit report with explainable checks
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
- public deployment
- mobile app
- email sending
- auto-apply
- AI chatbot interface as the primary UI

The v0 app may prepare a packet. It must not submit it.

## 6. Day-1 Success Criteria

After implementation, the user should be able to use CareerOps for a real application tomorrow.

Minimum successful path:

1. Start the local app.
2. Create one application from a JD.
3. Attach or create one LaTeX resume version.
4. Render a fresh one-page PDF.
5. See a fit report and risk report.
6. See all blocking issues before approval.
7. Approve the packet manually.
8. Export or open the PDF and report.

The system is not ready if any of these fail.

## 7. Functional Requirements

### FR1. Local Workspace

CareerOps must store all private data locally by default. Private profile data, resume text, generated answers, evidence, credentials, and scoring results must not be sent to a cloud service.

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

The system must extract:

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

The fit report must be transparent. It may include a local numeric score, but the score must be explained by checks.

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

## 9. Privacy Requirements

Private processing must stay local.

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
- approval cannot be bypassed

