# CareerOps System Design

Status: approved and implemented Application Desk v0.

## Runtime Shape

CareerOps is a Python 3.14 FastAPI application with a React 19 frontend, SQLite database, and local file vault. Pydantic validates API and provider contracts. The same FastAPI process serves the built frontend in one-origin mode and owns the background AI task runner.

The normal local layout is:

```text
Browser
  -> React workbench
  -> FastAPI API
       -> SQLite
       -> file vault
       -> LaTeX/PDF tools
       -> selected provider adapter
       -> deterministic verifier
```

Compose keeps the same application shape and adds storage initialization, managed Ollama bootstrap/runtime, a runtime inspector, and a GET-only Docker socket proxy.

## Durable Data

SQLite stores structured state; the vault stores private file bytes. The repository initializes and migrates the database, creates owned vault directories, reconciles AI artifact storage, and recovers expired task claims.

### Core Records

- `Application` owns company, role, location, source, current JD snapshot, selected resume, current report, approval, submission state, notes, and timestamps.
- `JobDescriptionSnapshot` points to the raw vaulted JD and checksum and holds parser/requirement groups.
- `ResumeVersion` points to vaulted `.tex` and optional PDF bytes, their checksums, parent ID, render/parse state, and page count.
- `EvidenceItem` and `ProfileFact` hold verification and privacy states.
- `Report` binds application, JD snapshot, and resume IDs to fit/risk JSON and blocker/warning counts.
- `Approval` binds application, resume, report, and PDF SHA-256.
- `ApplicationVersion` and `AuditEvent` preserve immutable workflow and before/after history.

### AI Records

- `AiProviderConfig` stores provider kind, display/identity name, model, privacy mode, endpoint, an availability/provenance binding, and its checksum.
- `AiTask` stores the application, task type, provider config, prompt ID/checksum, input IDs, privacy and provider binding snapshots, claim lease, invocation fence, status, and bounded error category.
- `AiArtifact` stores artifact type, vault path, checksum, schema version, and claim ownership.
- `AiFinding` stores claim/recommendation payload, severity, verifier state, and explicit user decision.

The API does not return private AI artifact paths. Repository reads revalidate path containment, regular-file/single-link identity, size, and checksum.

### Vault Layout

Private runtime directories include:

```text
data/careerops.db
vault/applications/
vault/resumes/<resume id>/
vault/evidence/
vault/reports/
vault/ai-tasks/<task id>/<attempt>/
vault/ai-artifacts/<task id>/<artifact id>/
vault/application-versions/
backups/
```

Provider attempt files are separate from published AI artifacts. Published artifacts use repository-owned paths and atomic copy/publication checks.

## Application Flow

### Create And Correct JD

The API writes the pasted JD to the vault and stores its checksum before returning the application. Deterministic parser `careerops-jd-parser-v1` populates the initial `deterministic` requirement group; `raw_ai`, `user_corrected`, and `uncertainty` begin separately. `analyze_jd` can add a raw AI group and uncertainty group. User corrections are stored separately and invalidate a current report based on older requirements.

### Import And Render Resume

Upload copies a single regular `.tex` file into a new resume version and selects it for the application. Rendering runs `latexmk` when available, otherwise `pdflatex`, inside a unique attempt directory. The adapter stores stdout/stderr in `render.log`, requires a PDF with extractable text on success, computes its SHA-256, parses its text, and records page count and status. The repository checks packet mutability before rendering and again when adopting the staged artifact, so a concurrent or repeated render cannot overwrite an approved PDF.

Risk analysis compares the current source bytes with the recorded source checksum. A mismatch is `PDF is stale` and blocks approval. Recovery restores the recorded source bytes or imports the corrected source as a new resume version before render and review are repeated.

### Run Task

1. The API validates application/provider/task compatibility and persists a `pending` task with provider and prompt bindings. A provider whose data leaves the machine also requires an explicit per-run acknowledgement; the boolean and server timestamp are immutable task authority.
2. The background runner submits the task. Repository compare-and-set changes one pending row to `running` with a claim token and lease.
3. A heartbeat renews the claim during provider execution and artifact/finding finalization.
4. The orchestrator snapshots current JD/resume identity and builds a bounded `ContextPacket` from task-relevant records. For `analyze_fit`, it stable-reads the rendered PDF, requires parsed nonempty text, and sends that extracted text with the exact PDF SHA-256 instead of reviewing the LaTeX source.
5. Prompt, context, and the exact checked-in JSON schema are written atomically to a task attempt directory and sealed into `ProviderRequest` snapshots.
6. The selected provider reports current availability. It must match the task's persisted provider binding before invocation.
7. The invocation fence is written before the external provider call. For Codex, the repository supplies only the latest thread for the same application, provider configuration, and exact provider-binding checksum.
8. Raw provider output is bounded, parsed as strict JSON, and validated against the selected task model. Codex JSONL must begin with a valid `thread.started` UUID; the UUID and `Started`, `Continued`, or `Rebuilt` mode are persisted on the task before verification.
9. Result metadata and invocation provenance must exactly match current availability and the persisted provider binding.
10. The deterministic verifier produces statement-specific blocked or `needs_evidence` findings. `analyze_fit_v2` separates provider blockers from warnings and retains score, matches, gaps, and recommendations in the report.
11. Context, raw output, validated output, and optional draft text are published as checksummed artifacts.
12. One repository transaction finalizes status, findings, artifacts, report/JD side effects, audit events, and application history only if the claim and current JD/resume IDs still match. Fit-report finalization also requires the selected resume's current PDF SHA-256 to match the immutable context; render drift aborts the report.

Terminal task states are `completed`, `blocked`, and `failed`. Public error summaries are limited to categories such as `provider_unavailable`, `provider_timeout`, `provider_error`, `invalid_output`, and `verification_failed`.

### Decide And Draft

Verified findings can move from `pending` to `accepted` or `rejected`; blocked findings cannot be accepted. Decisions are durable. Linking new evidence invalidates the current report and records an application version because the AI context changed. Both evidence-link paths reject mutation while any application AI task is pending or running, inside the same database write transaction, so an in-flight report cannot omit newly linked evidence. Evidence links also cannot mutate an approved or submitted packet.

Draft creation takes a completed `suggest_resume_edits` task and accepted verified finding IDs. It revalidates task/resume ownership, parent source checksum, evidence, rejected/private material, and exact single occurrence of each source span. All replacements are applied in memory and atomically published as a new child resume. The parent is not modified. Selecting the child clears current report/approval state and requires render/review again.

### Approve, Export, Submit

Approval revalidates report provenance, zero blockers, selected resume ID, current JD snapshot ID, PDF path and checksum, parse state, page count, extractable text, and current packet state from the same stable PDF bytes. The report must be the side effect of a completed `analyze_fit` task whose immutable context identifies `rendered_pdf` and carries the same PDF SHA-256; its persisted provider binding must also have `is_ai=true`. Verifier-only output and reports produced from a different PDF cannot authorize approval or the submission that depends on it. Approval records application ID, resume ID, report ID, and PDF SHA-256.

JD, PDF, report, and approval exports revalidate that each artifact is current. Manual submission checks the complete approval/report/resume/JD relationship and current PDF bytes before recording `submitted_manual`.

## Provider Boundaries

### Codex

Codex discovery is enabled only by `CAREEROPS_CODEX_ENABLED`. The ordered command list defaults to `codex2,codex`.

For `codex2`, CareerOps accepts only the approved `.cmd` descriptor that sets `%USERPROFILE%\codex-account2\.codex` and delegates to `codex`. For both identities it resolves a native fixed-drive `codex.exe`, rejects links/multiple links/unsafe size, streams its SHA-256 under a deadline, validates semver output, and requires exact ChatGPT login status.

Invocation uses the native executable directly with:

- `exec`
- ignored user config and rules plus strict config
- explicit model and reasoning effort
- no inherited shell environment
- read-only sandbox and a stable empty CareerOps-owned application workspace for a new thread
- no repository requirement
- JSONL thread events and persisted session files
- exact output schema and output file

Later calls use `codex exec resume <thread UUID> -` for only the latest matching application/provider binding. A failed resume gets one fresh invocation from the complete current context; a failed fresh invocation is terminal. Arguments are a list with `shell=False`; task content is bounded stdin. Only the leading bounded JSONL event is read from stdout for thread identity. The structured output file is read with path, link, size, and identity checks. The executable hash is checked before and after execution. Provider metadata records identity, endpoint, executable/hash/version, Codex home, model, effort, command, thread UUID/mode, and raw output checksum.

`codex2` and `codex` are separate records and never fallback within a task. Both are `cloud_external`; the UI requires a per-run transfer acknowledgement, and the API rejects a task unless that acknowledgement is present and persisted on the task.

### Managed Ollama

An `OllamaProvider` without verified isolation evidence returns unavailable. Its unverified status does not claim private processing.

In Compose, a temporary egress bootstrap installs the configured model into a persistent volume. Runtime Ollama is cloud-disabled, unexposed, and attached only to an internal bridge. The inspector obtains exact container and network state through a mutation-disabled Docker socket proxy. It returns a concrete container ID/handle, immutable image reference and image ID, start/network-derived runtime ID, model, endpoint, and no-egress policy.

In Windows mode, the PowerShell launcher copies the configured executable to an instance path, creates a journal, establishes a firewall block before process launch, starts `ollama.exe serve` with a reduced environment, and verifies executable hash/size, process start time, PID, loopback listener ownership, rule contents, and exact model. The runtime record uses a concrete `pid:<id>` handle.

`ManagedOllamaRuntimeFactory` validates either record, requires launcher reverification, generates an API-process authority, signs `ollama_isolation_v3` evidence, binds a live runtime verifier, probes exact local model metadata, and returns the provider plus capability. The capability includes endpoint, model, runtime instance/handle, optional container image identities, cloud-disabled and egress flags, issuer, and integrity signature.

Before availability and every run, the provider checks the authority signature and live verifier. It reads proxy-free `/api/tags` and `/api/show`, requires one exact local model name/digest/size, hashes both metadata responses, rejects remote/cloud fields, and calls `/api/chat` with `stream:false` and the JSON schema. It repeats model metadata checks after output validation. Any drift is unavailable or failed; only the verified path returns `local_private`.

### Deterministic Verifier

The deterministic provider implements the same typed task interface but has `is_ai=false`, `data_leaves_machine=false`, and `verifier_only`. It provides structured safety fallback output and human-review decisions without semantic model execution. Its report output is diagnostic only and is excluded from approval authority.

## Concurrency And Recovery

Task claims use compare-and-set row updates, claim tokens, finite leases, and heartbeats. Codex claims are serialized for the same application and exact provider binding so two workers cannot write the same conversation concurrently; other providers retain normal concurrency. Artifact rows carry claim ownership. Finalization verifies the active claim, current provider binding, current application state, and expected JD/resume IDs in one transaction.

Expired claims return to pending. Invocation fencing prevents automatic provider replay after an external call may have started. A stale worker cannot publish business state after losing its claim. Runner polling recovers pending work after restart. Historical report approval uses the immutable provider binding captured by the completed task; refreshing or disabling the current managed-runtime availability row after an API restart does not invalidate that historical authority.

File operations use exclusive creation, stable file identity checks, checksums, bounded reads, fsync, and atomic rename where required. The implementation rejects links, reparse points, hard links, path swaps, and changed identities at trust boundaries.

## Hosted Topology

The Compose services are:

- `storage-init`: no-network bind-mount ownership setup
- `ollama-bootstrap`: one-shot model check/download on temporary egress
- `ollama`: persistent cloud-disabled runtime on internal no-egress network
- `docker-socket-proxy`: digest-pinned GET-only Docker API boundary
- `runtime-inspector`: exact runtime/network/image attestation
- `careerops`: non-root, read-only, same-origin FastAPI/React application

`careerops` persists `/app/data`, `/app/vault`, and `/app/backups`; `ollama` persists its named model volume. Basic Auth protects private routes and `/health` remains public. HTTPS is supplied outside the container boundary.

The standard image does not install or authenticate Codex. Managed Ollama and the non-AI verifier have no paid cloud dependency. A custom hosted Codex installation remains external processing and requires the same per-task acknowledgement.

## Backup And Restore

Backup uses SQLite's online backup API for a consistent database file and a staged stable snapshot of vault files/directories. It validates database references to vaulted artifacts, rechecks source and staged identities/checksums, builds a canonical manifest, writes a bounded ZIP to an exclusive temporary file, computes archive SHA-256, and atomically publishes it. Concurrent mismatch aborts publication.

Restore binds one open archive descriptor, validates archive topology and limits, verifies the canonical manifest and every file, extracts to private staging directories, runs SQLite integrity checks, and snapshots staged topology. It then publishes data and vault through a durable two-parent transaction journal. Recovery verifies archive/manifest identity and target topology before committing or rolling back. Existing non-empty, changed, linked, or ambiguous targets are never replaced.

## Deferred Design

Cloud job watching, discovery, email ingestion, repository-wide autonomous review, automatic finding application, browser automation, and automatic submission are outside v0. Any later public watcher must not receive profile, resume, evidence, report, credentials, or private notes.
