# CareerOps AI-First Correction Design

Status: proposed correction for approval.

Date: 2026-07-21.

## 1. Correction

CareerOps must not be deterministic-only. AI is a core requirement.

The earlier v0 implementation treated "fully local/open-source resume analysis" as "no AI in the critical path." That interpretation is wrong. The corrected meaning is:

- AI is required for useful JD understanding, resume tailoring, semantic fit analysis, draft generation, and decision support.
- Private resume/profile processing should prefer local or user-controlled AI.
- AI output must be bounded by evidence, deterministic checks, version history, and user approval.
- Resume Worded must not be scraped or treated as a dependency.
- CareerOps must not claim to reproduce a proprietary ATS score.

This means CareerOps should become an AI-assisted career operations system with deterministic verification, not a deterministic resume checker.

## 2. Source Evidence

- Current CareerOps instruction included "fully local/open-source resume analysis" and local private processing decisions. Source: `C:\Users\nhnis\codex-account2\.codex\sessions\2026\07\21\rollout-2026-07-21T00-02-22-019f80cc-ef94-77b2-a5b5-827575b70644.jsonl`, line 7.
- User correction explicitly says "without ai, nothing will work" and asks whether Codex itself can be attached because context exists in `.codex` folders. Source: same JSONL, line 2214.
- Core-Subjects shows the user's preferred AI pattern: use LLMs for explanation, extraction, retrieval, and assistance, while keeping deterministic calculations, fallback paths, and safe phrasing. Sources:
  - `C:\Users\nhnis\Desktop\Core-Subjects\DE_SHAW_PROJECT_CODE_GRILLING_DOSSIER.md`, lines 79, 99, 123, 226, 262, 699, 784, 788.
  - `C:\Users\nhnis\Desktop\Core-Subjects\DE_SHAW_SYSTEMS_INTERN_30_DAY_DEEP_DIVE_PLAN.md`, lines 115, 121, 432, 853, 894.
  - `C:\Users\nhnis\Desktop\Core-Subjects\github-profile-readme\README.md`, lines 12, 25, 47.
- The Codex CLI is installed locally and ChatGPT-authenticated. `codex --help` exposes `exec`, `mcp-server`, `--oss`, and local provider support. `codex doctor` reports ChatGPT auth configured under `C:\Users\nhnis\codex-account2\.codex`; normal ChatGPT-authenticated inference is cloud-external.

## 3. Product Position

CareerOps is a private AI-assisted application desk for one user.

The app should feel like a calm operational workbench, not a chatbot. The user should not have to "chat from scratch" every time. AI should run inside explicit tasks:

- analyze this JD
- compare this resume to this JD
- suggest edits with evidence
- generate a role-specific resume draft
- generate a cover letter or recruiter reply
- inspect repository evidence before accepting a claim
- explain why a claim is risky
- decide which resume/application packet is safest to use

Each task has inputs, outputs, provider metadata, confidence, evidence links, and approval state.

## 4. Required AI Capabilities

### JD Intelligence

AI must extract and normalize:

- role family
- seniority and internship constraints
- required skills
- preferred skills
- responsibilities
- knockout conditions
- likely ATS keywords
- hidden expectations from wording
- India-specific application context when relevant

Deterministic parsing remains as fallback and verifier.

### Semantic Fit Analysis

AI must compare the JD against:

- rendered resume text
- verified profile facts
- evidence vault
- repository inspection records
- previous application history
- rejected or risky claims

It must explain:

- strong matches
- weak matches
- missing evidence
- likely overclaiming
- wording that sounds unnatural or AI-generated
- what to change first

### Resume Tailoring

AI must propose concrete LaTeX-safe edits:

- bullet rewrites
- skill ordering
- project ordering
- experience emphasis
- risk-safe title phrasing
- ATS keyword insertion without keyword stuffing
- one-page compression suggestions

The system must never silently overwrite the user's resume. AI edits create a draft resume version.

### Evidence-Locked Claim Review

AI may suggest a claim, but the deterministic gate decides whether it can be used.

Every claim should map to:

- verified evidence
- user-stated but not verified
- private and not publishable
- needs repository inspection
- rejected

Claims without acceptable evidence cannot pass approval.

### Draft Generation

AI may generate:

- cover letters
- recruiter replies
- LinkedIn message drafts
- application answers
- short "why this role" responses
- profile summaries

The copy must be non-bloated, direct, and not AI-slop. It must avoid invented metrics, unsupported claims, em dashes, excessive buzzwords, and generic motivation language.

### Codex-Assisted Deep Work

CareerOps can call the locally installed, ChatGPT-authenticated Codex CLI as an optional cloud-external worker for deeper tasks:

- inspect repository code before accepting project metrics
- reconstruct context from selected `.codex` sessions
- audit generated resume claims
- review application packet risk
- produce an evidence memo

This should be an explicit "Codex deep review" task, not a hidden dependency for every request.

## 5. Provider Architecture

The app should use a provider adapter layer.

Recommended order:

1. **Local OSS provider**: managed no-egress Ollama first, with LM Studio or another OpenAI-compatible local runtime later under an equivalent isolation contract. An arbitrary localhost daemon is not sufficient because it can offload cloud models. Best for private drafts, semantic analysis, and low/no recurring cost.
2. **Codex worker provider**: invokes a configured Codex CLI identity for deep repo/session reasoning. On this machine, `codex2` is the preferred identity and `codex` is also available. Best for evidence reconstruction and codebase inspection.
3. **Optional OpenAI API provider**: only when explicitly configured and approved. Best for higher quality generation when the user accepts cloud processing.
4. **Deterministic fallback provider**: existing rule-based parser/analyzer. This is fallback and verifier, not the full product.

Provider output must be normalized into structured JSON. The UI should show provider name, mode, timestamp, and whether private data left the machine.

## 6. Privacy Modes

CareerOps must make privacy mode explicit.

### Local Private Mode

- Resume text, profile facts, evidence, and generated drafts stay on the user's machine.
- Uses only a CareerOps-managed model runtime with cloud features disabled and OS/container egress blocked. The API process keeps a private authority key and issues an immutable HMAC-SHA256 capability bound to the canonical endpoint, exact model, runtime instance, and concrete process/container handle derived from the launched runtime. An unmanaged Ollama endpoint, environment assertion, mismatched capability, or capability from another authority is fail-closed and unavailable rather than labelled local-private.
- Does not include ChatGPT-authenticated Codex; that provider is cloud-external and requires explicit enablement.
- Best default for day-to-day work.

### ChatGPT-Authenticated Codex Worker Mode

- Uses the locally installed Codex CLI and its ChatGPT-authenticated cloud account.
- Can read selected local files and session files when explicitly scoped.
- Should run with a constrained working directory and explicit prompt contract.
- Must write artifacts back through CareerOps, not silently mutate user files.
- A normal ChatGPT-authenticated Codex run sends the bounded task context to OpenAI. The UI must say this before launch; it must not label Codex mode as fully local or claim that all task data stays on the machine.
- Multiple locally configured Codex identities are selectable. `codex2` is preferred on the current machine, with `codex` available as another explicit choice.
- CareerOps stores the chosen executable on the provider/task record and never silently falls back to a different Codex identity after a task starts.

### Cloud AI Mode

- Disabled by default.
- Requires explicit provider configuration and per-task approval for private data.
- The task record must say what data was sent and why.

### Hosted App Mode

Hosted CareerOps can run the UI/API container, database, and vault. AI processing should still prefer local private workers unless the user approves cloud AI in that hosted environment.

If hosted on a remote VM, private data lives on that VM. The app must say this clearly.

## 7. Low/No-Cost Uptime Architecture

The highest-uptime low-cost default is a self-contained Docker app with explicit AI workers, not a pile of hosted services.

Recommended shape:

- one FastAPI plus React container for the web app;
- SQLite plus vault files on persistent volumes;
- `/health` endpoint and container health check;
- `restart: unless-stopped` for VM/container restarts;
- scheduled backup of `data/` and `vault/`;
- AI tasks stored as resumable records before provider execution;
- managed no-egress local OSS AI worker created by `ManagedOllamaRuntimeFactory` in `careerops/operations/ollama_runtime.py` on the user's machine or trusted host;
- Codex worker only for explicit deep-review tasks;
- cloud AI disabled by default;
- hosted mode labels whether private processing is local, trusted-host, cloud AI, or verifier-only fallback.

This keeps the app hostable on an Always Free VM or cheap VPS while avoiding the biggest reliability risks: ephemeral storage, free database pause behavior, hidden paid AI dependencies, and browser-to-localhost fragility.

## 8. Data Model Additions

Add these records before implementing AI behavior:

### AiProviderConfig

- `id`
- `provider_kind`: `local_oss`, `codex_worker`, `openai_api`, `deterministic`
- `display_name`
- `enabled`
- `privacy_mode`
- `endpoint`
- `model`
- `created_at`
- `updated_at`

Secrets are never stored in plaintext in SQLite.

### AiTask

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

- `id`
- `task_id`
- `artifact_type`
- `path`
- `sha256`
- `schema_version`
- `created_at`

### AiFinding

- `id`
- `task_id`
- `finding_type`
- `severity`
- `claim_text`
- `evidence_item_id`
- `recommendation`
- `verifier_state`

### PromptTemplate

- `id`
- `name`
- `task_type`
- `version`
- `template_path`
- `sha256`
- `created_at`

Prompts are versioned artifacts. Changing a prompt changes the task provenance.

## 9. AI Task Flow

1. User creates or selects an application.
2. User selects an AI task, such as `Analyze JD` or `Tailor resume`.
3. Backend builds a bounded context packet:
   - JD snapshot
   - selected resume version
   - verified profile facts
   - relevant evidence only
   - rejected claims
   - user preferences
4. Provider adapter runs the model or Codex worker.
5. Model returns structured JSON plus draft text where needed.
6. Deterministic verifier checks:
   - schema validity
   - unsupported claims
   - stale resume/PDF
   - evidence mismatch
   - dangerous wording
   - secret-like text
7. App stores outputs as versioned artifacts.
8. UI shows AI suggestions beside verification status.
9. User accepts, edits, or rejects each suggestion.
10. Accepted suggestions create a new resume/application artifact version.
11. Approval can happen only after deterministic gates pass.

## 10. Codex Attachment Design

CareerOps should not depend on reading arbitrary `.codex` internals during normal app use.

The proper integration is a controlled Codex worker:

- The app prepares a task folder under `vault/ai-tasks/<task-id>/`.
- The task folder contains a bounded prompt, allowed source list, and output schema.
- CareerOps invokes `codex exec` with:
  - working directory set to CareerOps or a temporary task workspace
  - no destructive permissions by default
  - explicit output schema when practical
  - no silent modification of resume/application files
- Codex writes the final response to a task output artifact.
- CareerOps parses, stores, and verifies the output before showing it.

Codex executable discovery is interchangeable but explicit:

- `CAREEROPS_CODEX_COMMANDS` is an ordered comma-separated list, defaulting to `codex2,codex` for the current Windows installation.
- Each validated identity descriptor becomes a separate provider choice, labelled with its identity name, native executable, executable hash, CLI version, and explicit `CODEX_HOME`.
- `codex2` is the default when available. Its approved `.cmd` wrapper is validated only as an identity descriptor; production resolves and invokes the native `codex.exe` directly with `CODEX_HOME=C:\Users\nhnis\codex-account2\.codex`.
- `CAREEROPS_CODEX_COMMAND` may pin a single command for a deployment.
- If a selected identity becomes unavailable, that task fails with a recoverable provider error. It is not rerouted to another account.
- The worker uses read-only sandboxing, a bounded task directory, structured output, and ephemeral sessions. Prompt content goes through stdin; shell interpolation is not used.

Codex may be allowed to inspect selected `.codex` session files only through an explicit context reconstruction task. The app must never scan all 5+ GB of session history by default.

## 11. UI Changes

The UI remains minimal. Add one right-side or lower panel called `AI tasks`.

Required controls:

- provider status
- privacy mode
- task type
- run task
- output status
- accept suggestion
- reject suggestion
- create draft version

Avoid chat-first UX. A small task note box is acceptable for user instructions, but the primary UI remains structured.

Good labels:

- `Run AI review`
- `Local model`
- `Codex deep review`
- `Cloud AI disabled`
- `Private data stays local`
- `Creates draft only`
- `Needs evidence`
- `Blocked by verifier`

Bad labels:

- `AI magic`
- `Optimize my resume`
- `One-click ATS 95`
- `Guaranteed shortlist`
- `Autopilot apply`

## 12. Reliability Rules

- AI tasks are asynchronous and resumable.
- Provider failure must not corrupt application state.
- Every AI output has a schema version and checksum.
- AI edits create drafts, never overwrite approved artifacts.
- Verifier failure blocks approval even if AI says the packet is good.
- Prompt injection from JDs, resumes, or session files must be treated as untrusted input.
- Context packets must have size limits.
- Logs must not include raw secrets or full private resume text unless stored inside the private vault.
- The app must show when a result came from fallback deterministic checks rather than full AI.

## 13. Testing Requirements

Add tests for:

- provider config validation
- local provider unavailable fallback
- Codex worker command construction without broad destructive permissions
- AI output schema validation
- prompt injection handling from JD text
- unsupported claim generated by AI is blocked
- AI draft does not overwrite resume source
- accepted suggestion creates a new version
- cloud provider disabled by default
- privacy mode is visible in UI
- Playwright flow for JD -> AI review -> blocked suggestion -> accepted safe draft -> approval

Mock providers must be used for normal CI. Live provider tests should be opt-in.

## 14. Recommended Decisions For Approval

These are the best default choices for the first AI implementation:

1. First live local runtime: CareerOps-managed no-egress Ollama, because it is headless and fits a background app better than a desktop-first runtime. Task 10 implements `ManagedOllamaRuntimeFactory` in `careerops/operations/ollama_runtime.py`, `scripts/start-managed-ollama.ps1`, and `tests/test_managed_ollama_runtime.py`. The factory must own the runtime, disable cloud features, enforce an OS/container egress block, derive endpoint/model/runtime identity from the launched instance, retain the authority secret in the API process, and pass provider plus bound capability together. LM Studio can be added later only through an equivalent fail-closed isolation contract.
2. First implementation task: AI JD analysis plus semantic fit review, because resume tailoring and cover letters depend on understanding the role correctly.
3. Codex worker mode: implement after the local provider path is stable, then use it for evidence and repository deep review.
4. OpenAI API mode: keep documented but disabled and unimplemented in the first AI build unless explicitly approved later.
5. Secret storage for v1: do not store cloud AI secrets in SQLite. Use environment variables for optional providers. Managed Ollama needs no API secret, but endpoint/model environment values are not isolation evidence. The ChatGPT-authenticated Codex provider uses the selected CLI identity's explicit `CODEX_HOME` rather than app-managed credentials.
6. Normal CI: use mock providers only. Live local model and Codex tests are opt-in.
7. UI: add an `AI tasks` panel to the existing application desk, not a chatbot-first interface.

## 15. Remaining Open Decisions

These can remain open without blocking the first implementation:

1. Exact local model choice and size after Ollama is installed.
2. Whether the hosted VM should also run a CareerOps-managed no-egress Ollama runtime or only the web/API container.
3. Whether Windows Credential Manager should replace environment variables for future cloud provider secrets.
4. Which historical application artifacts should be migrated first after AI review works.

## 16. Recommendation

Implement AI in this order:

1. Data model and task framework.
2. Mock provider for tests.
3. Local OSS provider adapter.
4. JD AI analysis and semantic fit report.
5. Resume tailoring drafts.
6. Codex deep review for evidence and repository inspection.
7. Optional cloud AI provider after explicit approval.

This preserves the current reliable v0 as the verification layer while adding the AI capability that makes CareerOps useful.
