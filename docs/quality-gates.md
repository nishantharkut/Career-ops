# CareerOps Quality Gates

Status: required before implementation can be called complete.

Purpose: make old failure modes impossible to ship again.

## 1. Gate Rule

An application packet is not ready until every blocking gate passes.

Warnings can remain only when the report names the risk clearly and the user accepts it during approval.

## 2. Evidence Gates

Block approval when:

- a resume metric has no evidence record
- a project claim is marked `needs_verification`
- a rejected claim appears in a resume
- a private-only evidence item appears in public text
- a paper or research ownership claim is not verified
- a repository metric has not been inspected or manually verified

Warnings:

- claim is user-stated but not externally verified
- evidence exists but is old
- evidence is private and cannot be shown to recruiters

## 3. Resume Gates

Block approval when:

- LaTeX source changed after PDF render
- PDF is missing
- PDF checksum changed without a new report
- PDF has more than one page unless the application explicitly allows it
- PDF text extraction fails
- rendered text does not contain expected sections
- contact information is missing
- chronology is not reverse chronological
- role title conflicts with verified profile
- selected resume version is not linked to the application

Warnings:

- whitespace is high
- skills section is too generic
- bullets lack evidence
- role family mismatch is mild but not blocking

## 4. JD Gates

Block approval when:

- JD snapshot is missing
- source URL is missing and no manual JD text exists
- parser found a knockout condition that the profile does not satisfy
- internship year or graduation constraint appears incompatible
- location or work mode conflicts with stated constraints

Warnings:

- parser could not find eligibility rules
- work mode is unclear
- deadline is missing
- company name is inferred

## 5. Privacy Gates

Block approval when:

- public text names NDA-sensitive modules
- private repository names appear in public text without user approval
- generated artifact contains secret-like text
- logs include credential-like values
- cloud watcher tries to read profile, resume, evidence, or report data

The app must default to redaction when unsure.

## 6. Claim-Specific Gates From Reconstruction

The following known issues must be encoded as regression tests:

- QuickIntell must be represented as an internship, not full-time employment.
- QuickIntell public title must not be overclaimed without user approval.
- QuickIntell metrics such as `70+`, `108`, `40+`, `23+`, or percentage lifts must be verified before use.
- NDA-sensitive QuickIntell module names must not appear in public application text.
- `Covid-RARS` and `CuKD-XAI` paper-related claims must not imply completed publication unless verified.
- ISAC/RIS work must not be claimed as the user's work unless independently proven.
- Amazon HackOn and MLSS achievements can be used only in the exact verified form.
- Playwright should not be added to resume skills unless the user explicitly approves it for that application.
- Generic model-name dumping in skills is blocked.
- Keyword banners are blocked.

## 7. Approval Gates

Block approval when:

- report is older than selected PDF
- report was generated for a different JD snapshot
- selected resume changed after report
- approval record checksum does not match current PDF
- application has blocking risks

The user must be able to approve only after seeing:

- selected PDF
- fit report
- risk report
- blocking issue count
- warning count
- exact resume version

## 8. Submission Gates

The app must not submit applications in v0.

Manual submission record can be saved only after:

- application is approved
- user confirms they submitted outside the app
- timestamp and note are recorded

## 9. UI Gates

Block completion of the UI when:

- primary labels wrap awkwardly at 320, 375, 414, or 768 px
- any primary action lacks focus-visible state
- buttons or inputs are smaller than 44 px touch target
- important controls exist only on hover
- table data is unreadable on small screens
- the UI uses generic marketing copy
- there is a card inside a card without a real information reason
- there is a decorative hero or gradient-heavy layout
- status labels are vague

Required status labels:

- `Draft`
- `Needs evidence`
- `Needs resume update`
- `Ready to review`
- `Approved`
- `Submitted manually`
- `Archived`

## 10. Artifact Gates

Block completion when:

- report file is missing
- report does not include source IDs
- report cannot be traced to exact JD and resume version
- export packet omits approval record
- generated files overwrite user files silently

## 11. Test Gates

Before v0 is complete:

- all unit tests must pass
- all integration tests must pass
- Playwright day-1 workflow must pass
- PDF render and parse tests must pass
- secret scan must pass
- responsive screenshots must be inspected
- stale PDF fixture must fail before fix and pass after render
- invented metric fixture must block approval
- unsupported title fixture must block approval
- NDA wording fixture must block approval

## 12. Done Means

Done means the system can stop a bad application packet, not just generate a polished one.

