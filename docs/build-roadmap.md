# CareerOps Build Roadmap

Status: planning guide for goal mode.

This is not an implementation plan. It defines the correct build order so the e2e platform does not become unreliable.

## Phase 0. Repository And Test Harness

Goal: make the project safe to change.

Deliverables:

- git repository
- project structure
- dependency management
- test runner
- lint and format commands
- local config pattern
- private data ignored by git
- initial smoke test

Exit gate:

- tests run locally
- private data paths are ignored
- docs remain in the repo

## Phase 1. Core Data System

Goal: store applications, resumes, evidence, reports, approvals, and audit events.

Deliverables:

- SQLite database
- migrations
- Pydantic models
- repository layer
- application state transitions
- audit event writing
- backup and restore command

Exit gate:

- application state tests pass
- approval invalidation tests pass
- backup and restore test passes

## Phase 2. Application Desk UI

Goal: make the day-1 workflow usable.

Deliverables:

- local React UI
- application list
- application detail
- JD editor
- resume selector
- report panel
- approval panel
- plain operational copy

Exit gate:

- Playwright can create an application
- UI responsive checks pass
- no complex or AI-slop UI patterns

## Phase 3. Resume Render And Parse

Goal: connect LaTeX source to fresh PDF and parseable text.

Deliverables:

- LaTeX compiler adapter
- PDF checksum
- page count
- PDF text extraction
- stale PDF detection
- render log display

Exit gate:

- stale PDF fixture blocks approval
- fresh render passes
- broken LaTeX shows useful error

## Phase 4. Fit And Risk Reports

Goal: produce useful local analysis without fake proprietary scoring.

Deliverables:

- JD parser
- resume parser integration
- exact keyword report
- requirement coverage report
- evidence risk report
- blocker and warning system

Exit gate:

- invented metric fixture blocks approval
- NDA wording fixture blocks approval
- unsupported title fixture blocks approval
- report links to exact JD and resume version

## Phase 5. Evidence Vault And Repository Inspection

Goal: verify claims before they enter resumes.

Deliverables:

- evidence records
- claim records
- privacy levels
- repository inspection workflow
- branch and artifact notes
- metric verification status

Exit gate:

- unverified metric cannot be marked verified
- private evidence cannot leak into public text
- inspected repository evidence can support a claim

## Phase 6. Local Resume Analysis Improvements

Goal: improve report quality while staying local and open-source.

Deliverables:

- local NLP pipeline
- semantic skill matching
- knockout rule detection
- score calibration file
- report explanations

Exit gate:

- report remains explainable
- no cloud calls are required
- local score never claims to be Resume Worded or employer ATS

## Phase 7. Cloud Watcher

Goal: discover public opportunities without touching private data.

Deliverables:

- separate watcher service
- allowed source list
- public job metadata only
- notifications
- import-to-local workflow

Exit gate:

- watcher has no access to profile, resumes, evidence, or reports
- watcher output can be reviewed before importing

## Phase 8. Hardening

Goal: make the full platform reliable for daily use.

Deliverables:

- e2e tests
- visual checks
- artifact integrity checks
- secret scanning
- crash recovery
- documentation refresh

Exit gate:

- day-1 workflow passes
- full local application packet test passes
- UI checks pass on required widths
- all known historical failure fixtures pass

## Build Principle

Every phase must leave the product usable. Do not add a new subsystem if the previous packet workflow is broken.

