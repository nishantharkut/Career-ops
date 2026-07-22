# CareerOps Quality Gates

Status: implemented v0 gates and test coverage.

CareerOps treats provider output as a proposal. Schema validation, evidence checks, artifact integrity, and current packet identity remain authoritative.

## AI Contract And Provenance

The automated suite verifies that:

- only the five implemented task types and their bounded JSON schemas are accepted
- prompt and context files are stable, bounded, regular files inside the task directory
- provider output is bounded, strict JSON, schema-valid, and tied to a raw-output checksum
- provider availability and invocation metadata agree with the persisted provider binding
- task records retain provider ID, identity, model, privacy mode, prompt ID, prompt checksum, input IDs, and binding checksum
- context, raw output, validated output, and draft text artifacts are vault-contained and checksummed
- API artifact responses omit private vault paths and reject missing, stale, changed, or non-vault files
- provider errors are reduced to bounded categories without source data, credentials, or provider stderr

Codex tests cover separate `codex2` and `codex` identities, preferred ordering, exact wrapper validation, native executable hashing, ChatGPT login status, read-only argument-list invocation, isolated task directories, output bounds, timeout cleanup, and no fallback after identity selection.

Ollama tests cover authority-signed capabilities, exact endpoint/model/runtime binding, local model digest and metadata, pre-request runtime reverification, post-request provenance checks, remote/cloud metadata rejection, and fail-closed behavior without current isolation evidence.

## Evidence And Finding Decisions

The verifier and integration tests prove that:

- unsupported metrics, secrets, private material, rejected claims, and evidence mismatches become blocked findings
- blocked findings cannot be accepted
- verified findings require an explicit accepted or rejected decision
- claim review covers the exact supplied claim IDs
- private and unverified evidence is not silently promoted to publishable support
- user requirement corrections remain separate from raw AI output
- accepted exact resume edits must belong to one completed task and current parent resume
- each source span must occur exactly once and every replacement must pass evidence and privacy checks
- draft creation is atomic, creates a child, and leaves the parent bytes and checksum unchanged

The deterministic provider is tested as `is_ai=false` and `verifier_only`. UI tests require `Run verifier` and reject AI labelling for this fallback.

Approval tests require the current report to come from a completed `analyze_fit` task with a persisted `is_ai=true` provider binding. Verifier-only output cannot authorize approval or manual submission.

## Concurrency And Recovery

Repository, orchestrator, API, and task-runner tests cover:

- compare-and-set claiming so one pending task has one active claim
- finite claim leases and heartbeat renewal during provider and post-provider work
- expired-claim recovery without allowing a stale worker to commit
- invocation fencing so uncertain interrupted calls are not silently invoked twice
- provider-binding drift between enqueue and run
- concurrent requirement/resume/application changes during finalization
- atomic task status, findings, artifacts, report side effects, and application version events
- runner startup recovery, background polling, queue pressure, unhealthy runner handoff, and clean shutdown
- artifact path, hard-link, identity-swap, and publication races
- draft parent/source races and approved/submitted packet mutation refusal

An interrupted pending task is eligible for polling again. A task whose provider invocation may already have started can fail closed with `provider_error` rather than replaying the external call.

## Resume, Report, Approval, And Submission

Tests verify:

- local LaTeX discovery, staged rendering, successful nonempty PDF/text parsing, and a retained render log on failure
- stale source, missing/unparseable PDF, unsupported metrics, unverified claims, NDA-sensitive wording, and unsupported titles block the deterministic report
- a report is bound to application ID, JD snapshot ID, and selected resume ID
- `analyze_fit` context uses extracted text from the exact rendered PDF and records its SHA-256
- fit-report finalization rejects a PDF checksum change after context capture
- requirement, evidence, or resume selection changes invalidate the current report and approval state
- create-and-link and existing-item link operations are atomic no-ops while AI work is pending or running
- approval requires the current zero-blocker report, matches its context PDF checksum, and reparses the exact current PDF bytes
- approval rejects a report without completed AI-backed `analyze_fit` provenance, including verifier-only output
- approval stores application ID, resume ID, report ID, and PDF SHA-256
- export routes reject stale report/approval ownership or changed PDF bytes
- manual submission is impossible before approval and remains bound to the approved packet
- reload restores selected resume, parent/child history, task provenance, findings, approval, and manual submission state
- cloud tasks reject missing per-run transfer acknowledgement and persist its server timestamp
- managed-runtime report approval and submission survive API restarts without trusting refreshed availability as historical evidence

The mock-provider Playwright flow exercises create application, analyze JD, save a correction, add evidence and a profile fact, render risky and clean resumes, block an altered unsupported metric with exact statement diagnostics, create a verified child draft, rerender and rerun, export artifacts, review claims, approve, submit manually, reload, and inspect history. An opt-in real-resume case imports the configured `.tex`, verifies its source SHA-256, renders and exports a one-page PDF, and completes fit review.

## Managed Runtime And Hosting

Hosting tests parse the shipped Compose configuration and verify:

- same-origin static serving and configurable development API origin
- Basic Auth on private routes, public `/health`, and fail-fast required credentials
- persistent app data, vault, backup, and model mounts
- restart and health policies
- non-root/read-only app and inspector settings
- no bundled API key or authority key
- one-shot storage initialization for exact bind mounts
- one-shot model bootstrap on a temporary egress network
- runtime Ollama attached only to an internal no-egress network with cloud disabled and no host port
- digest-pinned Ollama and Docker socket proxy images
- GET-only Docker API access with mutation surfaces disabled
- inspector checks for exact container, image, network, environment, model, and runtime identity
- API-issued capabilities rather than environment assertions

Windows runtime tests verify the app-owned executable copy, reduced child environment, outbound firewall rule, loopback listener ownership, PID/start-time/executable identity, exact model availability, launch journal recovery, and cleanup ordering. Cleanup removes isolation only after the owned process and listener are proven gone.

## Backup And Restore

Backup tests verify:

- a consistent SQLite online snapshot during writes
- stable vault capture plus database-to-vault path/checksum validation
- bounded file count, member size, total size, archive size, manifest size, and compression ratio
- manifest, archive, and per-file SHA-256 values
- source, staged-file, archive-path, link, hard-link, and identity-swap refusal
- no archive publication when concurrent vault bytes or topology change
- strict archive topology with no traversal, absolute, duplicate, case-colliding, symlink, or undeclared entries
- explicit existing empty restore targets that are separate and non-link
- SQLite integrity and extracted-file checksum verification before publication
- two-target durable journal recovery and rollback across every publication phase
- refusal to delete or overwrite a changed target after an interrupted restore
- PowerShell wrappers with explicit absolute paths and no expression evaluation

## Commands

Normal provider, runtime, hosting, and backup coverage:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_ai_providers.py `
  tests\test_ai_context_and_verifier.py `
  tests\test_ai_orchestrator.py `
  tests\test_ai_api_flow.py `
  tests\test_resume_drafts.py `
  tests\test_managed_ollama_runtime.py `
  tests\test_hosting_configuration.py `
  tests\test_backup_restore.py -q
```

UI and browser gates:

```powershell
npm run test:web
npm run build:web
npm run test:e2e
```

Normal pytest excludes real providers through the configured `not live_provider` expression. Explicit marker selection is the opt-in:

```powershell
.\.venv\Scripts\python.exe -m pytest -m live_provider -q
```

The live tests use only synthetic JD fixture data under `tmp_path`. Codex selects `codex2` only and never falls back to another identity. The Codex smoke starts a persisted thread and proves a second structured task resumes the exact UUID. Managed Ollama runs only through an explicitly configured managed runtime that has passed attestation; otherwise it skips with an unavailable reason.

## Release Gate

Before a release or merge:

1. Run the normal Python, web, build, and Playwright suites.
2. Run the live marker on a machine with the intended real provider identities.
3. Run the stale-claim consistency scan from Task 11.
4. Run `git diff --check` and inspect the owned-file diff.
5. Confirm no `.env`, credentials, runtime data, vault data, backups, model data, or provider output entered source control.

The release is blocked by a failed normal test, unexpected live-provider failure, unreviewed privacy/provenance drift, stale packet export, or backup/restore integrity failure.
