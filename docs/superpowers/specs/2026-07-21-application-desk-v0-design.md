# Application Desk v0 Design

Status: approved direction, awaiting implementation planning.

Source docs:

- `docs/context-reconstruction.md`
- `docs/prd.md`
- `docs/system-design.md`
- `docs/quality-gates.md`
- `docs/day-1-workflow.md`
- `docs/ui-design-principles.md`

## 1. Design Summary

Application Desk v0 is the first CareerOps product slice. It is a local application workbench that helps the user prepare one safe application packet at a time.

The system must not behave like an unbounded chat. It must store explicit records, preserve versions, require evidence, render fresh PDFs, produce local reports, and block approval when risks are present.

## 2. Architecture

Use a local web architecture:

- FastAPI backend
- SQLite database
- local file vault
- React TypeScript frontend
- local LaTeX compiler adapter
- local parsers and analyzers
- pytest and Playwright tests

Private data stays local. The future cloud watcher remains separate and public-metadata-only.

## 3. Components

Core components:

- application ledger
- resume version registry
- evidence vault
- JD snapshot store
- resume compiler
- PDF parser
- JD parser
- fit analyzer
- risk gate engine
- approval engine
- audit log
- local UI

Each component owns one responsibility and exposes a small interface.

## 4. Data Flow

Primary flow:

1. User saves a JD.
2. Parser extracts requirements.
3. User selects or imports a LaTeX resume.
4. Compiler renders a PDF.
5. Parser extracts PDF text.
6. Analyzer compares JD, resume, and evidence.
7. Risk gates decide blockers and warnings.
8. User reviews report.
9. User approves packet.
10. User records manual submission after submitting outside the app.

Approval is invalidated by any later change to JD, resume source, rendered PDF, or report.

## 5. Error Handling

Errors must be direct and useful.

Examples:

- `PDF render failed. Open the render log.`
- `Claim has no evidence. Link evidence or remove the claim.`
- `PDF is stale. Render again before approval.`
- `NDA-sensitive wording found. Rewrite before approval.`

No generic failure messages.

## 6. UI Design

Use a minimal operational UI:

- application list
- selected application detail
- JD and requirements
- resume status
- checks and approval panel

Do not build:

- marketing hero
- decorative dashboard
- complex navigation
- AI chatbot shell
- gradient-heavy interface

Use Hallmark as an audit standard for anti-slop UI:

- honest copy
- no invented metrics
- no fake proof
- no stock SaaS layout
- complete interaction states
- responsive checks

CareerOps should be plain, calm, and usable.

## 7. Testing

Required test categories:

- model tests
- state transition tests
- parser tests
- render tests
- stale PDF tests
- risk gate tests
- approval invalidation tests
- API integration tests
- Playwright day-1 workflow
- responsive UI checks
- secret scan
- backup and restore test

Known historical failures must be test fixtures.

## 8. Scope Boundaries

v0 does not include:

- automatic job discovery
- cloud watcher
- auto-submit
- Resume Worded automation
- public deployment
- multi-user auth
- mobile app

These can be added only after the local application packet workflow is reliable.

## 9. Implementation Readiness

Implementation may start only after:

- docs are reviewed
- unresolved product decisions are accepted or deferred
- the implementation plan is written
- test gates are mapped to concrete test files

The first implementation goal should complete Application Desk v0 before expanding the platform.

