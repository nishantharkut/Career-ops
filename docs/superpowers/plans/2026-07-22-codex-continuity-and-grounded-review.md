# Codex Continuity and Grounded Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the live CareerOps AI workflow continuous, actionable, and approval-safe from JD analysis through fit review.

**Architecture:** Persist the Codex thread ID on each AI task and resume only the latest thread for the same application and exact provider binding. Keep the full current context packet authoritative on every turn. Replace fit risk ambiguity with an `analyze_fit_v2` blockers/warnings contract, make analysis verification statement-specific, and render the complete report in the existing workbench.

**Tech Stack:** Python 3.13, FastAPI, SQLite migrations, Pydantic, Codex CLI JSONL, React 19, TypeScript, Vitest, Playwright.

---

### Task 1: Persist AI Conversation Metadata

**Files:**
- Modify: `careerops/core/migrations.py`
- Modify: `careerops/core/models.py`
- Modify: `careerops/core/repository.py`
- Modify: `apps/api/careerops_api/main.py`
- Test: `tests/test_migrations.py`
- Test: `tests/test_ai_repository.py`

- [ ] Write failing migration tests for nullable thread fields on existing rows and strict validation on new values.
- [ ] Write failing repository tests for recording a thread on a claimed task, retrieving only the latest same-application/same-binding thread, and rejecting a concurrent pending/running task for the same application/provider.
- [ ] Add migration 19 with `provider_thread_id` and `provider_thread_mode` columns plus finite-state triggers.
- [ ] Add thread fields to `AiTask`, row conversion, and API payloads.
- [ ] Add repository methods to retrieve and record bound thread metadata.
- [ ] Run focused migration and repository tests.

### Task 2: Run New and Resumed Codex Conversations

**Files:**
- Modify: `careerops/ai/contracts.py`
- Modify: `careerops/ai/providers/codex.py`
- Modify: `apps/api/careerops_api/main.py`
- Test: `tests/test_ai_providers.py`

- [ ] Write failing tests for request continuity fields and invocation thread metadata.
- [ ] Write failing provider tests for a new `codex exec --json` conversation without `--ephemeral`.
- [ ] Write failing provider tests for `codex exec resume <thread-id>`, one fresh fallback after resume failure, malformed/missing `thread.started`, provider identity drift, and attempt cleanup.
- [ ] Add a stable, empty, CareerOps-owned session workspace per application/provider identity.
- [ ] Parse the leading Codex JSONL `thread.started` event with a bounded capture.
- [ ] Preserve read-only sandboxing, ignored user rules/config, strict schema output, identity checks, and typed sanitized failures.
- [ ] Pass the repository vault session root into discovered Codex providers.
- [ ] Run focused provider tests.

### Task 3: Connect Continuity Through the Orchestrator

**Files:**
- Modify: `careerops/ai/orchestrator.py`
- Test: `tests/test_ai_orchestrator.py`
- Test: `tests/test_ai_api_flow.py`

- [ ] Write failing tests proving the first task starts a thread, the second task receives it, different applications/providers do not share it, and blocked analysis still records continuity.
- [ ] Add application continuity key and prior bound thread ID to `ProviderRequest`.
- [ ] Validate provider-returned thread metadata and persist it before terminal finalization.
- [ ] Keep non-Codex providers thread-free.
- [ ] Run focused orchestrator and API tests.

### Task 4: Introduce `analyze_fit_v2`

**Files:**
- Create: `careerops/ai/prompts/analyze_fit_v2.txt`
- Modify: `careerops/ai/context.py`
- Modify: `careerops/ai/contracts.py`
- Modify: `careerops/ai/orchestrator.py`
- Test: `tests/test_ai_providers.py`
- Test: `tests/test_ai_context_and_verifier.py`
- Test: `tests/test_ai_orchestrator.py`

- [ ] Write failing schema tests requiring separate `blockers` and `warnings` collections.
- [ ] Write failing prompt-version tests expecting `analyze_fit_v2` and its checksum.
- [ ] Add the v2 prompt with conservative blocker/warning definitions and current-context authority.
- [ ] Replace the ambiguous `risks` output field with blockers and warnings.
- [ ] Persist blockers and warnings unchanged into report risk groups.
- [ ] Update test fixtures and run focused contract/orchestrator tests.

### Task 5: Make Analysis Verification Actionable

**Files:**
- Modify: `careerops/ai/verifier.py`
- Modify: `careerops/ai/orchestrator.py`
- Test: `tests/test_ai_context_and_verifier.py`
- Test: `tests/test_ai_orchestrator.py`

- [ ] Add a failing regression using the real Google-style “must include a resume” wording and resume-only 109/164 test metrics.
- [ ] Add failing tests for exact finding text/field/index, semantic analysis warnings, and continued blocking of fabricated matches, negated/learning-only skills, unsupported metrics, private/rejected material, and prompt injection.
- [ ] Narrow source-directive recognition so ordinary applicant instructions are not treated as model instructions.
- [ ] Use overlap grounding for semantic analysis while retaining strict publication-time verification.
- [ ] Allow analysis-only `needs_evidence` findings without marking the task failed.
- [ ] Attach exact claim text and output field to every analysis finding.
- [ ] Correct fit metric authority by section.
- [ ] Run the full verifier and orchestrator test modules.

### Task 6: Show Complete Review and Continuity in the UI

**Files:**
- Modify: `apps/web/src/api.ts`
- Modify: `apps/web/src/components/AiTasksPanel.tsx`
- Modify: `apps/web/src/components/FindingsPanel.tsx`
- Modify: `apps/web/src/components/ReviewPanel.tsx`
- Modify: `apps/web/src/styles.css`
- Test: `apps/web/src/AiTasksPanel.test.tsx`
- Test: `apps/web/src/App.test.tsx`

- [ ] Write failing component tests for context status, exact warning text, no reject action on analysis warnings, fit score, matches, gaps, blockers, warnings, and recommendations.
- [ ] Extend the task/report API types with thread metadata and fit v2 fields.
- [ ] Render plain context continuity labels in task metadata.
- [ ] Render verifier state labels by actual state instead of calling every non-verified finding blocked.
- [ ] Render the complete fit report in the Checks panel with compact unframed sections.
- [ ] Keep blocker-based approval disabling and existing responsive layout.
- [ ] Run web tests and the production build.

### Task 7: Browser and Live Provider Verification

**Files:**
- Modify: `tests/e2e/ai-application-flow.spec.ts`
- Modify: `tests/e2e/application-desk.spec.ts` only if a focused visual/state assertion belongs there.
- Modify: `docs/day-1-workflow.md`

- [ ] Add Playwright assertions for the complete fit report, exact warning state, blocker-gated approval, reload persistence, and context status.
- [ ] Run the complete backend suite.
- [ ] Run the complete web suite and production build.
- [ ] Stop the hosted development process, run the complete Playwright suite against its isolated database, and restart the hosted app with Codex enabled.
- [ ] Run an opt-in live `codex2` JD task followed by a fit task and prove the second task resumes the same thread.
- [ ] Inspect desktop and mobile screenshots for clipping, overlap, blank states, and independent scrolling.
- [ ] Update the operating guide with the exact workflow and continuity behavior.
- [ ] Confirm `git diff --check`, repository status, and the live health endpoint.
