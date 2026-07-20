# CareerOps UI Design Principles

Status: required UI direction for v0.

Inputs:

- user preference for clean, minimal UI
- Hallmark anti-AI-slop rules
- CareerOps PRD
- prior failures from context reconstruction

## 1. UI Position

CareerOps is an operational tool. It is not a landing page, not a chatbot wrapper, and not a motivational dashboard.

The UI should feel like a private workbench:

- calm
- dense
- readable
- plain
- fast to scan
- hard to misuse

No complex UI unless a workflow truly needs it.

## 2. Tone

Tone: utilitarian and austere.

Use plain labels. Do not use marketing copy or AI product language.

Good labels:

- `Add application`
- `Render PDF`
- `Analyze`
- `Missing evidence`
- `Claim not verified`
- `PDF is stale`
- `Needs approval`
- `Approve packet`
- `Mark submitted manually`

Bad labels:

- `Unlock opportunities`
- `Supercharge your workflow`
- `AI-powered optimization`
- `Seamless application intelligence`
- `Transform your career journey`
- `Smart insights`

## 3. First Screen

The first screen is the application desk.

Layout:

- left rail: applications
- center pane: selected application and JD
- right pane: checks, resume status, reports, and approval

No hero section. No welcome banner. No decorative empty dashboard.

When empty, show:

`No applications yet. Add a JD to start a packet.`

Button:

`Add application`

## 4. Navigation

Use a small, plain navigation structure:

- `Applications`
- `Resumes`
- `Evidence`
- `Reports`
- `Settings`

Do not use a generic SaaS nav with marketing links. No pricing, blog, resources, or CTA nav.

## 5. Visual Rules

Use:

- one tinted paper background
- one ink color
- one quiet accent
- restrained borders
- stable spacing
- tabular numbers for scores and counts
- clear status chips

Avoid:

- purple-blue gradients
- gradient text
- giant hero typography
- decorative orbs
- fake browser chrome
- fake phone frames
- nested cards
- equal three-column feature grids
- icon tiles as decoration
- celebratory animations

Accent use should be rare. Use it for focus, active state, and small status signals.

## 6. Component Rules

Every interactive component must have:

- default state
- hover state
- focus-visible state
- active state
- disabled state
- loading state when async
- error state when validation can fail
- success state when completion is not obvious

Touch targets must be at least 44 by 44 CSS pixels.

Focus rings must be visible and instant.

Hover-only controls are not allowed.

## 7. Forms

Forms must use:

- visible labels above inputs
- helper text below inputs
- error text in the same place as helper text
- stable input height
- stable helper text height
- validation on blur
- revalidation after touched fields change

Placeholder text shows format, not instruction.

Good:

- label: `Deadline`
- placeholder: `21 Jul 2026`

Bad:

- placeholder: `Enter deadline here`

## 8. Status Language

Status must be specific.

Use:

- `Draft`
- `Analyzing`
- `Needs evidence`
- `Needs resume update`
- `Ready to review`
- `Approved`
- `Submitted manually`
- `Archived`

Do not use vague states such as:

- `Optimized`
- `Good to go`
- `Enhanced`
- `AI ready`

## 9. Reports

Reports should be easy to scan:

- blockers first
- warnings second
- matched requirements third
- recommended edits last

Each issue needs:

- label
- severity
- source
- why it matters
- next action

No long paragraphs unless the user opens details.

## 10. Motion

Default: almost no motion.

Allowed:

- short loading indication
- small pressed state on buttons
- drawer or modal open transition

Rules:

- animate transform and opacity only
- support reduced motion
- no bounce
- no parallax
- no cursor effects
- no scroll-triggered fade on every section
- no celebratory success effects

## 11. Responsive Requirements

The UI must work at:

- 320 px
- 375 px
- 414 px
- 768 px
- desktop width

Required:

- no horizontal scroll
- no primary button label wraps
- no unreadable table on mobile
- panes stack cleanly on small screens
- controls remain reachable

If a table becomes too wide, convert it to a compact record list on small screens.

## 12. Copy Review

Before completion, scan UI text for:

- marketing claims
- AI buzzwords
- vague labels
- unexplained jargon
- motivational filler
- generic error text

Replace with plain action language.

Examples:

- `Something went wrong` becomes `PDF render failed. Open the render log.`
- `Submit` becomes `Approve packet` or `Save application`
- `Continue` becomes the exact next action

## 13. Hallmark Use

Hallmark should be used as an audit layer, not as a decorative style generator.

For CareerOps, Hallmark means:

- honest copy
- no invented metrics
- no gradient-heavy AI look
- no stock SaaS structure
- no fake proof
- token-based design
- full interaction states
- responsive verification

The product should still look like a focused internal tool.

