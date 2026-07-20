# CareerOps Day-1 Workflow

Status: target workflow for the first usable release.

Purpose: define the exact path the user should be able to use for a real application tomorrow.

## 1. Start

User starts the local app.

Expected screen:

- application list on the left
- selected application or empty state in the center
- checks and approval panel on the right

If no applications exist, the empty state says:

`No applications yet. Add a JD to start a packet.`

Primary action:

`Add application`

## 2. Add Application

User enters:

- company
- role title
- source URL if available
- JD text
- deadline if known
- location if known

The app saves the raw JD snapshot before parsing.

The user should see:

- `JD saved`
- extracted requirements
- fields marked `Unknown` when not found

The system must not invent missing company, location, or eligibility details.

## 3. Review Extracted Requirements

The app shows extracted groups:

- required skills
- preferred skills
- responsibilities
- eligibility
- location and work mode
- knockout conditions
- exact keywords

User can edit extracted fields. Edits are saved as user corrections.

The system records both:

- raw parser output
- user-corrected requirements

## 4. Select Resume

User chooses:

- existing resume version
- duplicate and tailor an existing version
- import a LaTeX source

For v0, the app should support importing an existing `.tex` file and registering it as a resume version.

The app must show:

- source path
- last source checksum
- PDF path
- render status
- PDF freshness
- parse status
- page count

If source changed after render, the app shows:

`PDF is stale. Render again before approval.`

## 5. Render PDF

User clicks:

`Render PDF`

The compiler adapter runs the local LaTeX command.

Success state:

- PDF path
- PDF checksum
- page count
- parse status

Failure state:

- short error
- render log link
- no approval allowed

The app must not hide LaTeX errors.

## 6. Analyze Fit

User clicks:

`Analyze`

The app generates:

- fit report
- risk report
- recommended edits

Fit report shows:

- strong matches
- partial matches
- missing requirements
- exact keyword gaps
- evidence confidence
- knockout risks

Risk report shows:

- blockers
- warnings
- source for each issue
- suggested next action

## 7. Fix Issues

The user handles blockers:

- add evidence
- remove or rewrite a claim
- update LaTeX
- rerender PDF
- rerun analysis

The app must keep the previous report for history. It must not overwrite a report silently.

## 8. Review Packet

When there are no blockers, application status becomes:

`Ready to review`

The review panel shows:

- exact company and role
- selected resume version
- PDF checksum
- report ID
- warning count
- approval button

The approval button label:

`Approve packet`

## 9. Approve

When user approves, the app records:

- application ID
- resume version ID
- report ID
- PDF checksum
- timestamp
- user note if entered

Status becomes:

`Approved`

If any input changes later, approval resets.

## 10. Manual Submission

The user submits outside the app.

Afterward the user clicks:

`Mark submitted manually`

The app records:

- submitted timestamp
- submission note
- final artifact references

Status becomes:

`Submitted manually`

## 11. Expected Time

For a normal JD and existing resume:

- add application: 3 to 5 minutes
- review requirements: 5 to 10 minutes
- render and analyze: 2 to 5 minutes
- fix issues: depends on blockers
- approval: 2 minutes

The goal is not instant automation. The goal is fewer unsafe submissions.

## 12. Day-1 Acceptance Test

The implementation must ship with an automated test that does this:

1. Creates an application from a sample JD.
2. Imports a sample LaTeX resume.
3. Renders a PDF.
4. Parses the PDF text.
5. Generates fit and risk reports.
6. Blocks approval when a fixture claim lacks evidence.
7. Passes after the claim is fixed or evidence is linked.
8. Approves the packet.
9. Marks it submitted manually.

The test must run locally.

