# CareerOps Codex Continuity and Grounded Review Design

Status: approved under the user's standing instruction to make the best reliability-first choices and proceed without repeated confirmation.

Date: 2026-07-22.

## 1. Problem

The first real Google application exposed three connected defects:

1. Every Codex task uses `codex exec --ephemeral`, so no Codex conversation survives between tasks.
2. The deterministic verifier applies extractive string matching to semantic analysis. It blocks reasonable paraphrases and reports only generic `unsupported_claim` or `unsupported_metric` labels.
3. A successful fit report is not fully visible in the workbench. Its score, matches, gaps, and recommendations are stored but omitted from the UI. The current fit contract also puts every risk in the warning list, so an explicit eligibility conflict cannot reliably block approval.

The live failed task is useful evidence for the defect. Codex correctly identified the enrollment conflict and cited metrics already present in the rendered resume, while the verifier rejected the report because its prose did not appear as an ordered source substring.

## 2. Decisions

### Per-application Codex continuity

CareerOps will keep one Codex conversation for each `(application, provider configuration)` pair.

- Google and Stripe never share a model conversation.
- `codex2` and `codex` never share a conversation or silently substitute for one another.
- New tasks resume the most recent recorded thread for the same application and exact provider binding.
- Every task still sends a complete current context packet. Saved conversation history is helpful continuity, not the source of truth.
- If a recorded thread cannot resume, the provider may create one replacement thread and rerun the read-only task from the current context packet.
- The task record stores the thread ID and whether the run started, continued, or rebuilt the conversation.
- Pending and running tasks for the same application/provider are serialized so two workers cannot write to one Codex thread concurrently.

This is intentionally not one global CareerOps chat. A global thread would mix employers, roles, resume versions, and rejected claims, and would become less reliable as it grows.

### Durable authority

The context packet remains authoritative on every turn and contains the current selected inputs:

- JD snapshot;
- selected resume source or verified rendered PDF text;
- profile facts;
- linked evidence;
- rejected and private claim classifications;
- writing preferences.

The prompt continues to tell Codex to analyze only the delimited current packet. A resumed thread must not reuse a stale claim that is absent from the current packet. If local Codex session files disappear, CareerOps loses conversational convenience, not the ability to perform a correct task.

### Analysis verification

Analysis and publication have different risk levels.

- Resume edits, cover letters, claim reviews, private material, secrets, unsupported metrics, source-directive echoes, and rejected claims remain fail-closed.
- JD and fit analysis may contain conservative semantic paraphrases and derived recommendations. A lexical mismatch becomes an explicit `needs_evidence` warning tied to the exact output field and text instead of blocking the whole report.
- A fit match with no grounding in either the JD or the candidate material remains blocking.
- Metrics in matches may be supported by the candidate resume; they do not also need to appear in the JD. Metrics in gaps, blockers, warnings, and recommendations may be supported by either relevant source.
- Ordinary application instructions such as "Applicants must include a resume" are not prompt injection. Explicit model-facing directives remain excluded and fail-closed.

### Fit contract

`analyze_fit_v2` separates:

- `blockers`: explicit qualification conflicts, publication risks, or conditions that should prevent packet approval;
- `warnings`: uncertainty, missing confirmation, or non-fatal weaknesses.

The report persists those groups without reclassifying all risks as warnings. Approval remains disabled while blockers exist.

### UI

The existing three-column workbench stays intact.

- Task metadata shows `Context` as `Started for this application`, `Continued for this application`, or `Rebuilt from current application data`.
- Verification findings show the exact affected text and a plain state label. Analysis warnings are not presented as rejectable resume suggestions.
- The Checks panel shows fit score, matches, gaps, blockers, warnings, and recommendations.
- The UI uses existing typography, spacing, colors, controls, and responsive behavior. No chat pane, decorative cards, marketing copy, or new visual system is added.

## 3. Data and command flow

1. The orchestrator looks up the latest valid thread ID for the application and provider binding.
2. It seals the same bounded provider request used today and adds the application continuity key and optional thread ID.
3. A new conversation runs `codex exec --json` without `--ephemeral` in a stable empty CareerOps-owned workspace.
4. A continuing conversation runs `codex exec resume <thread-id> --json` with the current schema and prompt on stdin.
5. CareerOps parses only the leading `thread.started` JSONL event for the thread ID. It does not persist progress output.
6. The normal structured output file is schema-validated and deterministically verified.
7. The task row records thread ID/mode before terminal finalization. The report and application version retain their existing checksum and provider authority.
8. On a resume failure, one fresh read-only invocation is allowed. Other failures remain typed and fail without changing the application packet.

## 4. Failure handling

- Missing or malformed thread metadata: fail the provider run without accepting output.
- Missing historical thread: recreate once from current context and record `rebuilt`.
- Concurrent task for the same application/provider: reject enqueue with a recoverable conflict.
- Provider identity, executable, model, or binding drift: do not resume the old thread and do not switch accounts.
- Invalid schema or unsafe output: preserve the existing blocked-task behavior.
- Semantic analysis warning: complete the task, persist the report, show the exact warning, and leave the final decision to the user.
- Explicit blocker: complete the fit report with blockers visible and keep approval disabled.

## 5. Verification

Required coverage:

- migration and repository tests for thread metadata, binding isolation, and task serialization;
- Codex provider tests for new, resumed, rebuilt, malformed JSONL, cleanup, and unchanged identity controls;
- verifier tests using the real Google-style application wording and candidate-only resume metrics;
- orchestrator tests proving blockers and warnings are persisted separately;
- component tests for complete fit report rendering, context state, and actionable finding labels;
- API and Playwright coverage for JD analysis, fit analysis, blocker gating, reload, and provider continuity;
- full backend, web, production build, and browser suites;
- opt-in live `codex2` run that starts a conversation and a second run that resumes it.

## 6. Non-goals

- No general chatbot UI.
- No global cross-application Codex thread.
- No automatic application submission.
- No weakening of publication-time resume and claim verification.
- No dependence on arbitrary `.codex` transcript scanning for normal operation.
