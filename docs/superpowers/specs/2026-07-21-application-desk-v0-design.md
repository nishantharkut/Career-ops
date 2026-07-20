# Application Desk v0 Design

Status: implemented deterministic baseline. Superseded for AI requirements by `docs/superpowers/specs/2026-07-21-ai-first-careerops-design.md`.

Source docs:

- `docs/context-reconstruction.md`
- `docs/prd.md`
- `docs/system-design.md`
- `docs/quality-gates.md`
- `docs/day-1-workflow.md`
- `docs/ui-design-principles.md`
- `docs/superpowers/specs/2026-07-21-ai-first-careerops-design.md`

## Correction Note

This v0 design was useful as a reliability slice, but it is not the final CareerOps direction. It treated local/open-source analysis as deterministic-only analysis. The user later corrected that AI is mandatory.

For future work:

- keep the Application Desk, repository, render, parse, fit, and risk-gate pieces as the verifier foundation;
- add AI task orchestration, local OSS model support, and Codex deep-review support;
- keep deterministic checks as safety gates after AI output;
- do not present deterministic-only reports as the full product.

## 1. Design Summary

Application Desk v0 is the first CareerOps product slice. It is a local application workbench that helps the user prepare one safe application packet at a time.

The system must not behave like an unbounded chat. It must store explicit records, preserve versions, require evidence, render fresh PDFs, produce reports, and block approval when risks are present.

After the AI-first correction, this desk should become the structured shell in which AI tasks run. AI suggestions create drafts and findings; deterministic gates decide whether those outputs are safe enough for approval.

## 2. Architecture

Use a local web architecture:

- FastAPI backend
- SQLite database
- local file vault
- React TypeScript frontend
- local LaTeX compiler adapter
- local parsers and analyzers
- AI task orchestrator in the next phase
- local OSS AI provider in the next phase
- optional Codex worker for deep review in the next phase
- pytest and Playwright tests

Private data stays local by default. The future cloud watcher remains separate and public-metadata-only. Optional cloud AI remains disabled unless explicitly configured and approved.

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
- AI task framework
- provider adapter layer
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
6. AI task compares JD, resume, profile facts, and evidence when a provider is configured.
7. Deterministic analyzer and verifier check AI output, JD, resume, and evidence.
8. Risk gates decide blockers and warnings.
9. User reviews AI suggestions, report, and verifier findings.
10. Accepted suggestions create draft versions only.
11. User approves packet.
12. User records manual submission after submitting outside the app.

Approval is invalidated by any later change to JD, resume source, rendered PDF, or report.

## 5. Error Handling

Errors must be direct and useful.

Examples:

- `PDF render failed. Open the render log.`
- `Claim has no evidence. Link evidence or remove the claim.`
- `PDF is stale. Render again before approval.`
- `NDA-sensitive wording found. Rewrite before approval.`
- `AI provider unavailable. Run verifier only or configure a local model.`
- `AI suggestion has no evidence. Link evidence or reject it.`

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
- chat-first AI shell
- gradient-heavy interface

Use Hallmark as an audit standard for anti-slop UI:

- honest copy
- no invented metrics
- no fake proof
- no stock SaaS layout
- complete interaction states
- responsive checks
- explicit AI privacy labels
- provider status that does not hide fallback behavior

CareerOps should be plain, calm, and usable.

## 7. Testing

Required test categories:

- model tests
- state transition tests
- parser tests
- render tests
- stale PDF tests
- risk gate tests
- AI task model tests
- mock provider tests
- AI output verifier tests
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

The deterministic implementation also does not yet include AI. That is a known gap, not an accepted final boundary. The next approved design is the AI-first correction spec.

## 9. Implementation Readiness

Implementation may start only after:

- docs are reviewed
- unresolved product decisions are accepted or deferred
- the implementation plan is written
- test gates are mapped to concrete test files

The first implementation goal should complete Application Desk v0 before expanding the platform.
