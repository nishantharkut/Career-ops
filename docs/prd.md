# CareerOps PRD

Status: approved and implemented Application Desk v0.

## Product

CareerOps is a private, single-user application desk for India-focused internship applications. It turns a pasted job description and a LaTeX resume into a reviewed application packet with durable evidence, AI provenance, deterministic safety checks, explicit approval, export, and manual submission history.

The product is a workbench, not a chat interface or submission bot. It does not claim to predict hiring outcomes.

CareerOps does not reproduce, scrape, calibrate to, or claim equivalence with Resume Worded or any proprietary employer ATS score. Its fit score is a local, explainable aid tied to visible matches, gaps, risks, and recommendations. It is not a `90-95` promise, an employer score, or a hiring outcome.

## User And Operating Assumptions

The v0 user:

- applies primarily to internships in India
- works with LaTeX resumes
- needs company, role, location, eligibility, skills, and evidence checked without invented facts
- wants private local AI where a verified managed runtime is available
- may use a selected ChatGPT-authenticated Codex identity after acknowledging external transfer
- reviews every finding and submits on the employer's site

Local use is the default. A self-hosted single-user deployment is supported when Basic Auth, HTTPS, persistent storage, and backups are in place.

## Implemented Scope

### Application Packet

The UI creates an application from company, role title, optional location, optional source URL, and pasted JD text. CareerOps stores an immutable raw JD file and checksum, parsed requirements, user corrections, selected resume, latest report, approval, submission state, and version history.

The current statuses are `draft`, `analyzing`, `needs_evidence`, `needs_resume_update`, `ready_for_review`, `approved`, `submitted_manual`, and `archived`.

### Evidence

The user can create and link evidence records and create profile facts. Each item has an explicit verification state and privacy level. The verifier distinguishes verified, user-stated, needs-verification, rejected, and private-do-not-publish material. A model cannot promote unsupported or private material into accepted public wording.

### Resume Versions

The UI uploads a `.tex` file into the CareerOps vault, records its source checksum, renders locally through `latexmk` or `pdflatex`, parses the PDF text, and records PDF checksum, page count, render status, and parse status.

AI resume edits are exact source replacements. Accepted verified edits create a child resume version atomically. The selected parent remains unchanged. A child begins without a PDF and must be rendered and reviewed again.

### Structured AI Tasks

The five implemented task types are:

- `analyze_jd`
- `analyze_fit`
- `suggest_resume_edits`
- `draft_cover_letter`
- `review_claims`

Each task is durable before execution. It records application ID, provider configuration ID, privacy mode, model/provider binding, prompt ID and checksum, input artifact IDs, status, claim lease, invocation state, completion time, and a bounded error category.

The context builder selects only task-relevant JD, resume, verified facts, linked evidence, rejected claims, and writing preferences. It normalizes and bounds every collection, redacts credentials and restricted material, marks source text untrusted, and seals prompt, context, and schema snapshots before invocation.

Output must match the checked-in schema for the selected task. Raw and validated output are checksummed vault artifacts. Findings retain verifier state and user decision.

### Provider Modes

#### ChatGPT-Authenticated Codex

`CAREEROPS_CODEX_ENABLED=true` enables discovery. `CAREEROPS_CODEX_COMMANDS=codex2,codex` registers identities in that order when each passes descriptor, executable, version, and ChatGPT login checks. `codex2` is preferred; `codex` is a separate provider. A selected task never falls back to another identity.

Codex uses a native, hashed `codex.exe`, an isolated attempt directory, read-only sandbox settings, a reduced environment, structured output schema, bounded stdin/output, and a finite timeout. The current default model is `gpt-5.6-sol` with `xhigh` reasoning effort.

Codex is `cloud_external` and `data_leaves_machine=true`. The UI requires a fresh acknowledgement for each launch and shows the selected identity, model, privacy mode, task, prompt ID, and checksum.

#### Verified Managed Ollama

Compose bootstraps the configured model into a persistent volume through a temporary bootstrap-only egress network. Runtime Ollama has cloud features disabled, no host port, and only an internal no-egress network.

The Docker socket proxy is GET-only. The inspector attests the exact Compose container, image, process start, model setting, private address, and internal network. The API process issues a signed capability bound to that runtime. The provider reverifies the runtime and local model before every request and model provenance after generation.

Windows process mode copies the configured executable into an app-owned runtime instance, starts only a loopback listener with cloud disabled, installs and verifies a program-specific outbound firewall block, binds PID/start time/executable identity, and cleans up the exact process and firewall rule at shutdown. It requires the exact model to already exist in the configured runtime root.

Only a successfully verified managed runtime is `local_private`. A generic or unmanaged Ollama endpoint remains unavailable.

#### Deterministic Verifier

The deterministic provider is available without an AI runtime. It performs useful safety and human-review checks with `is_ai=false`, `verifier_only`, and no data transfer. The UI labels its command `Run verifier`. It is not represented as semantic AI review, and its output cannot authorize approval or submission.

### Review And Approval

The UI polls pending/running tasks, shows completed or blocked findings, and requires an explicit accept/reject decision. Blocked findings cannot be accepted.

`analyze_fit` materializes a report bound to the current application, JD snapshot, and resume. Deterministic gates cover stale or missing PDF state, parse failure, evidence gaps, unsupported metrics/titles, private or NDA-sensitive wording, rejected claims, and secret-like material. The local fit value is diagnostic and accompanied by matches, gaps, risks, and recommendations; it is not an employer result.

Approval is available only when the current zero-blocker report was materialized by a completed `analyze_fit` task whose persisted provider binding has `is_ai=true`, and the selected resume has a current rendered PDF. A verifier-only report is diagnostic and cannot satisfy this authority check. The approval stores application ID, resume version ID, report ID, PDF SHA-256, timestamp, and note. Current JD ownership is enforced through the report binding.

The user exports the current JD, resume PDF, report, and approval. Artifact endpoints verify current ownership and bytes before download.

After submitting outside CareerOps, the user selects `Mark submitted manually`. Submission is refused without a valid current approval.

### History And Recovery

Application versions, audit events, task records, artifacts, findings, and decisions survive browser reload and API restart. Task claims use compare-and-set updates, finite leases, heartbeats, and invocation fencing. Expired work is recovered without letting a stale worker commit or replaying an uncertain external invocation.

## Privacy And Hosting

Private state lives in SQLite and the local vault. Production-style serving uses one FastAPI/React origin. Compose requires Basic Auth, keeps `/health` public, persists `data`, `vault`, `backups`, and the Ollama model volume, and applies health/restart policies.

HTTPS is required before real data crosses a network. A hosted provider processes data on the host where it is reachable. The standard Linux image includes managed Ollama but not a Codex CLI or authenticated Codex home. Codex in a custom hosted image remains external cloud processing.

There is no bundled API key, paid scoring dependency, cloud database requirement, remote resume service, or hidden AI fallback.

## Backup And Restore

The shipped PowerShell backup command takes explicit backup-root, data, and vault paths. It creates a stable SQLite online snapshot, stages and revalidates the vault, checks database artifact bindings, and atomically publishes a bounded ZIP with archive, manifest, and file checksums. Concurrent mismatch fails without publication.

Restore takes an explicit archive and two separate existing empty target directories. It rejects unsafe archive topology and path races, verifies checksums and SQLite integrity, and uses a durable journal for two-target recovery. It never overwrites non-empty targets.

## Acceptance State

The implemented browser and API tests cover this complete path:

1. Create an application from a synthetic JD.
2. Analyze the JD and save a correction.
3. Add verified evidence and a profile fact.
4. Upload and render a risky resume.
5. Block and reject an unsupported provider claim.
6. Upload and render a clean parent resume.
7. Run fit review and exact resume-edit suggestions.
8. Accept a verified edit and create an unchanged-parent child draft.
9. Render and review the child.
10. Export the JD, PDF, and report.
11. Persist cover-letter provenance and review claims.
12. Approve exact current IDs and PDF checksum.
13. Export the approval and record manual submission.
14. Reload and verify packet, provenance, finding decisions, and history.

Live smoke coverage is excluded from normal pytest. Explicit `-m live_provider` runs a synthetic real `codex2` invocation and a managed Ollama invocation only when an attested fixture is configured.

## Out Of Scope And Deferred

The following are not implemented in v0:

- cloud job watcher or automatic discovery
- email ingestion or alerts
- repository-wide autonomous inspection
- automatic acceptance or application of findings
- browser form filling
- auto-apply or automatic submission
- scraping paid resume-analysis sites
- employer outcome prediction
- multi-user accounts or public unauthenticated hosting
- general cloud AI API adapters

Any later watcher is limited to public job metadata and must stay separate from private profile, resume, evidence, report, credentials, and notes.
