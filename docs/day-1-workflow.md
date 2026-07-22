# CareerOps Day-1 Workflow

Status: implemented v0 workflow.

This is the browser path for one India-focused internship application. CareerOps prepares and records the packet; the user submits it outside the app.

## 1. Create The Application

1. Open the workbench and select `Add application`.
2. Enter company, role title, and JD text. Location and source URL are optional.
3. Select `Save application`.

CareerOps writes the raw JD to the vault, stores its checksum in SQLite, creates an application record, and shows it in the Applications rail. The implemented UI imports a JD by pasted text; it does not fetch or scrape the source URL.

## 2. Analyze And Correct The JD

1. In `AI review`, choose a provider.
2. Choose `Analyze JD` and select `Analyze JD`.
3. Watch the persisted task move through `Pending`, `Running`, and `Completed` or a terminal error state. The browser polls every 1.5 seconds.
4. Open the `Requirements` tab. `Raw AI` contains the structured provider result.
5. Select `Add correction`, enter the corrected key and value, then select `Save corrections`.

Corrections are stored separately under `User corrected`; the raw AI group remains in history. Saving corrections invalidates a report that was based on older requirements.

## 3. Add Evidence And Profile Facts

1. Open `Evidence`.
2. Create an evidence item with claim, type, optional source URL, verification state, and privacy level.
3. Add a profile fact with its verification and privacy state.
4. Link existing evidence by ID when needed.

Only evidence and facts admitted by the bounded context builder are sent to a provider. Rejected, private, or unverified material remains available to deterministic verification and redaction rules but is not silently treated as publishable support.

Wait for pending/running AI work to finish before linking new evidence. CareerOps rejects evidence-link mutations while a task is active so an in-flight review cannot omit a newly linked item; rerun the intended task after the evidence is linked.

## 4. Import And Render A Resume

1. Open `Resume`.
2. Enter a resume name and choose a `.tex` file.
3. Select `Upload resume`. CareerOps copies the source into its vault and selects the new version.
4. Select `Render PDF`.

The renderer uses local `latexmk` or `pdflatex`, writes `render.log`, stores the PDF checksum, parses the text layer, and records page count and render/parse status. A failed render has no usable PDF and cannot produce an approvable packet.

The recorded source checksum is the PDF freshness boundary. If the vaulted source changes after import, the next analysis reports `PDF is stale`. Do not edit the vaulted copy. Restore its recorded bytes or upload the corrected `.tex` as a new resume version, then render and rerun.

`Analyze fit` reads the text extracted from this rendered PDF, not the LaTeX source. Its persisted context records the PDF SHA-256. Replacing or rerendering the PDF after the run starts prevents that run from creating an approvable report.

## 5. Choose A Provider And Task

The task menu contains:

- `Analyze fit` with command `Run AI review`
- `Analyze JD`
- `Suggest resume edits`
- `Draft cover letter`
- `Review claims`

The provider menu shows only registered provider records. `Codex account 2 (codex2)` is preferred when available; `Codex default (codex)` remains a separate choice. A task never changes to another identity after launch.

For Codex, the panel states `Selected task data is sent through your signed-in Codex account`. Check `Acknowledge Codex data transfer for this run` before each launch. Codex is `cloud_external` and receives only the selected task's bounded prompt and context. CareerOps records that one-run acknowledgement on the task; direct requests without it are rejected.

Codex continuity is scoped to the application and exact selected account/provider binding. The first successful task shows `Context: Started`; later tasks for that same application and binding show `Continued`. `Rebuilt` means the saved thread was unavailable and CareerOps made one fresh read-only invocation using the complete current context. A different application or Codex identity starts a different conversation. CareerOps replays the complete current context on every turn; conversation memory cannot authorize a stale or unsupported claim.

Verified managed Ollama is `local_private`. Unmanaged Ollama is unavailable rather than being trusted from a localhost address. `Deterministic verifier` uses `Run verifier` and is explicitly not AI.

## 6. Launch And Inspect Provenance

Select the task command once. The run button is disabled while the task is pending or running. The status section shows:

- provider identity
- model
- privacy mode
- task type and task ID
- context state (`Started`, `Continued`, `Rebuilt`, or `Not started`)
- prompt template ID
- prompt checksum

Provider binding, invocation metadata, context, raw output, validated output, and artifact checksums are persisted. A provider timeout, unavailable provider, malformed output, binding drift, or failed verifier leaves a terminal task record without silently applying content.

## 7. Review Findings

Completed and blocked tasks show persisted findings:

- Actionable `Verified suggestion` and claim-review findings can be accepted or rejected.
- `Needs evidence` identifies an advisory analysis sentence that should be checked but does not invalidate the whole private report.
- `Blocked by verifier` identifies the exact unsafe output sentence and cannot be accepted. Analysis diagnostics do not show fake suggestion-decision controls.
- A saved decision remains after reload.

AI confidence does not override schema, evidence, privacy, secret, rejected-claim, or exact-edit checks.

## 8. Create A Child Resume Draft

1. Run `Suggest resume edits` against the selected rendered resume.
2. Accept at least one exact verified edit.
3. Enter a draft name and select `Create resume draft`.

CareerOps checks that every selected finding belongs to the completed task and selected parent, is verified and accepted, and names source text that occurs exactly once. It applies all accepted replacements atomically to a new child `.tex` source. The parent resume ID, bytes, and checksum do not change.

The child becomes selected with no PDF. The UI reports `Draft created. Render it before review.`

## 9. Render And Rerun Review

1. Select `Render PDF` for the child.
2. Run `Run AI review` again.
3. Resolve every blocker by uploading corrected source, creating another verified child, correcting requirements, adding evidence, or selecting another resume.
4. Render and rerun whenever a new child or newly uploaded resume becomes selected.

The current report is bound to the current application, JD snapshot, and selected resume. Changing requirements or the selected resume invalidates the previous current report. A report with blockers keeps `Approve packet` disabled.

## 10. Review Claims

1. Choose `Review claims`.
2. Launch the task against the stored profile facts and linked evidence.
3. Inspect each supported, unsupported, private, or needs-review decision.
4. Accept or reject each verified finding explicitly.

A model decision is a finding, not an automatic profile edit.

## 11. Approve The Exact Packet

When the latest report has zero blockers, select `Approve packet`.

The API rechecks that:

- the report came from a completed AI-backed `analyze_fit` task, not the verifier-only provider
- the report belongs to the application
- the report's resume ID is the selected resume ID
- the report's JD snapshot ID is the current JD snapshot ID
- the fit-analysis context identifies the rendered PDF and carries the report's PDF SHA-256
- the rendered PDF exists and its current checksum matches the resume record
- the report has no blocking issues
- no prior approval or submission already closes the packet

The approval record stores the application ID, resume version ID, report ID, PDF SHA-256, approval timestamp, and note. Later packet mutation is rejected or invalidates current review state; it is not folded into the approval.

## 12. Export

The `Exports` section downloads available current artifacts:

- `Export JD`
- `Export resume PDF`
- `Export report`
- `Export approval` after approval

Artifact routes verify current ownership and checksums before serving files. A stale report or approval returns a conflict instead of exporting an old packet as current.

## 13. Submit Manually

Submit the application on the employer's site. CareerOps does not open, fill, or submit that site.

After submission, select `Mark submitted manually`. This action is enabled only after approval and records the timestamp and note against the unchanged approved packet.

## 14. Reload And History

Reload the browser and select the application again. The packet endpoint restores application state, selected parent/child resume relationship, latest task, findings and decisions, report, approval, evidence links, and submission state.

Open `History` to inspect immutable application versions such as task completion, requirement correction, draft creation, approval, and manual submission.

## Blockers And Recovery

- **No provider:** Choose the verifier for safety checks or configure Codex/managed Ollama. The verifier is not an AI substitute and cannot authorize approval or submission.
- **Codex acknowledgement missing:** The run command stays disabled. Check the per-run acknowledgement only after reviewing the selected provider and task.
- **Task failed:** Read `error_summary`; fix availability, timeout, or output shape, then launch a new task. Do not switch identities inside the failed task.
- **Blocked finding:** Reject it or add/correct evidence and run a new task. It cannot be accepted.
- **LaTeX failed:** Inspect `vault\resumes\<resume id>\render\render.log`, fix the original source outside CareerOps, upload it as a new resume version, and render that version.
- **PDF stale:** Restore the vaulted source to its recorded bytes or upload the current source as a new resume version, then render and rerun. The stale PDF/report is not approvable.
- **Interrupted task:** Reload. Pending tasks are repolled and expired claims are recovered. Invocation fencing can convert an uncertain interrupted call to `provider_error` instead of invoking it twice.
- **Approval refused:** Confirm the selected child is rendered, the current report was produced for that resume and JD, and the blocker count is zero.
- **Submission refused:** Approve the exact current packet first.
