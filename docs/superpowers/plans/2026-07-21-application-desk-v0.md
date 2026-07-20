# Application Desk v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local CareerOps Application Desk v0 that can create a job application packet, import and render a LaTeX resume, parse the PDF, generate fit and risk reports, block unsafe approval, approve a clean packet, and mark it submitted manually.

**Architecture:** Use a FastAPI backend with SQLite and a local file vault, plus a React TypeScript Vite frontend. The backend owns all state transitions, checksums, rendering, parsing, reports, and approval gates. The frontend is a minimal operational workbench that calls only the local API.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, SQLite, pytest, pypdf, React, TypeScript, Vite, Vitest, Playwright, local MiKTeX `latexmk` or `pdflatex`.

---

## File Structure

- Create `pyproject.toml`: Python dependencies, pytest config, package metadata.
- Create `package.json`: workspace scripts for backend tests, frontend tests, Playwright, and dev servers.
- Create `apps/api/careerops_api/`: FastAPI entrypoint and routers.
- Create `careerops/core/`: domain models, database, repositories, state transitions, audit logs.
- Create `careerops/parsers/`: deterministic JD parser and PDF resume parser.
- Create `careerops/renderers/`: local LaTeX compiler adapter.
- Create `careerops/analyzers/`: fit analyzer and score explanation.
- Create `careerops/gates/`: blocking and warning risk gates.
- Create `apps/web/`: React TypeScript UI.
- Create `tests/`: backend unit and integration tests.
- Create `apps/web/tests/`: frontend unit tests.
- Create `tests/e2e/`: Playwright day-1 workflow tests.
- Create `fixtures/`: sample JD, LaTeX resume, and risk cases.

## Task 1: Project Harness

**Files:**
- Create: `pyproject.toml`
- Create: `package.json`
- Create: `careerops/__init__.py`
- Create: `tests/test_smoke.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write the smoke test**

```python
# tests/test_smoke.py
def test_imports_package():
    import careerops

    assert careerops.__version__ == "0.1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_smoke.py -q`

Expected: fail because `careerops` is not installed or `__version__` is missing.

- [ ] **Step 3: Add package and dependency configuration**

Create `pyproject.toml` with:

```toml
[build-system]
requires = ["setuptools>=69", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "careerops"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = [
  "fastapi>=0.116",
  "uvicorn[standard]>=0.35",
  "pydantic>=2.11",
  "python-multipart>=0.0.20",
  "pypdf>=5.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.4",
  "httpx>=0.28",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

Create `careerops/__init__.py` with:

```python
__version__ = "0.1.0"
```

Create root `package.json` with:

```json
{
  "scripts": {
    "test:api": "python -m pytest -q",
    "dev:api": "python -m uvicorn apps.api.careerops_api.main:app --reload --host 127.0.0.1 --port 8765",
    "dev:web": "npm --prefix apps/web run dev -- --host 127.0.0.1 --port 5173",
    "test:web": "npm --prefix apps/web run test -- --run",
    "test:e2e": "npx playwright test",
    "test:all": "npm run test:api && npm run test:web && npm run test:e2e"
  },
  "devDependencies": {
    "@playwright/test": "^1.55.0"
  }
}
```

- [ ] **Step 4: Install dependencies and run smoke test**

Run: `python -m pip install -e ".[dev]"`

Run: `npm install`

Run: `python -m pytest tests/test_smoke.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml package.json package-lock.json careerops tests .gitignore
git commit -m "chore: add project harness"
```

## Task 2: Domain Models And SQLite Store

**Files:**
- Create: `careerops/core/models.py`
- Create: `careerops/core/status.py`
- Create: `careerops/core/database.py`
- Create: `careerops/core/repository.py`
- Test: `tests/test_repository.py`

- [ ] **Step 1: Write repository tests**

Tests must prove:

- application creation persists JD snapshot metadata
- resume versions are separate from applications
- approval is invalidated when selected resume changes
- audit events are written

Run: `python -m pytest tests/test_repository.py -q`

Expected: fail because modules do not exist.

- [ ] **Step 2: Implement SQLite schema**

Create tables:

- `applications`
- `jd_snapshots`
- `resume_versions`
- `evidence_items`
- `reports`
- `approvals`
- `audit_events`

Use `sqlite3` from the standard library. Use row factories and explicit transactions.

- [ ] **Step 3: Implement repository methods**

Required methods:

- `create_application(company, role_title, location, source_url, jd_text)`
- `list_applications()`
- `get_application(application_id)`
- `create_resume_version(name, role_family, tex_path, tex_sha256)`
- `select_resume(application_id, resume_version_id)`
- `save_report(application_id, resume_version_id, jd_snapshot_id, fit, risks)`
- `approve_application(application_id, report_id, note)`
- `mark_submitted_manual(application_id, note)`

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_repository.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add careerops/core tests/test_repository.py
git commit -m "feat: add local application repository"
```

## Task 3: JD Parser

**Files:**
- Create: `careerops/parsers/jd_parser.py`
- Test: `tests/test_jd_parser.py`
- Create: `fixtures/sample_jd.txt`

- [ ] **Step 1: Write parser tests**

Tests must prove extraction of:

- required skills
- preferred skills
- responsibilities
- eligibility
- location
- work mode
- exact keywords
- unknown fields stay `Unknown`

Run: `python -m pytest tests/test_jd_parser.py -q`

Expected: fail because parser does not exist.

- [ ] **Step 2: Implement deterministic parser**

Use heading and keyword heuristics. Do not call cloud APIs. Return a structured object with lists and `Unknown` for missing scalar fields.

- [ ] **Step 3: Run parser tests**

Run: `python -m pytest tests/test_jd_parser.py -q`

Expected: pass.

- [ ] **Step 4: Commit**

```powershell
git add careerops/parsers fixtures/sample_jd.txt tests/test_jd_parser.py
git commit -m "feat: add deterministic JD parser"
```

## Task 4: LaTeX Renderer And PDF Parser

**Files:**
- Create: `careerops/renderers/latex.py`
- Create: `careerops/parsers/pdf_parser.py`
- Create: `fixtures/sample_resume.tex`
- Test: `tests/test_resume_render_parse.py`

- [ ] **Step 1: Write render and parse tests**

Tests must prove:

- renderer discovers `latexmk` or `pdflatex`
- sample LaTeX renders a PDF
- PDF checksum is recorded
- page count is one
- extracted text contains `Nishant Harkut`, `QuickIntell`, and `PocketBuddy`
- broken LaTeX returns a clear failure

Run: `python -m pytest tests/test_resume_render_parse.py -q`

Expected: fail because renderer does not exist.

- [ ] **Step 2: Implement renderer**

Implementation:

- prefer `latexmk -pdf -interaction=nonstopmode -halt-on-error`
- fallback to `pdflatex -interaction=nonstopmode -halt-on-error`
- run inside an isolated output directory
- capture stdout and stderr to `render.log`
- return success, pdf path, log path, checksum, and error summary

- [ ] **Step 3: Implement PDF parser**

Use `pypdf.PdfReader` for page count and text extraction.

- [ ] **Step 4: Run render tests**

Run: `python -m pytest tests/test_resume_render_parse.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add careerops/renderers careerops/parsers fixtures/sample_resume.tex tests/test_resume_render_parse.py
git commit -m "feat: render and parse LaTeX resumes"
```

## Task 5: Fit Analyzer And Risk Gates

**Files:**
- Create: `careerops/analyzers/fit.py`
- Create: `careerops/gates/risk.py`
- Test: `tests/test_fit_and_risk.py`

- [ ] **Step 1: Write analyzer and gate tests**

Tests must prove:

- exact keyword matches are reported
- missing requirements are reported
- unverified metric blocks approval
- NDA-sensitive wording blocks approval
- unsupported QuickIntell title blocks approval
- stale PDF blocks approval
- clean fixture has zero blockers

Run: `python -m pytest tests/test_fit_and_risk.py -q`

Expected: fail because analyzer and gates do not exist.

- [ ] **Step 2: Implement fit analyzer**

Return:

- matched requirements
- missing requirements
- weak matches
- exact keyword gaps
- local score parts
- explanation strings

- [ ] **Step 3: Implement risk gate engine**

Risk issue fields:

- `severity`
- `label`
- `source`
- `why`
- `next_action`

Blocking labels must include:

- `Missing evidence`
- `PDF is stale`
- `Claim not verified`
- `NDA-sensitive wording`
- `Unsupported title`
- `Secret-like text`

- [ ] **Step 4: Run analyzer tests**

Run: `python -m pytest tests/test_fit_and_risk.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add careerops/analyzers careerops/gates tests/test_fit_and_risk.py
git commit -m "feat: add fit analyzer and risk gates"
```

## Task 6: FastAPI Endpoints

**Files:**
- Create: `apps/api/careerops_api/main.py`
- Create: `apps/api/careerops_api/schemas.py`
- Create: `apps/api/careerops_api/routes.py`
- Test: `tests/test_api_flow.py`

- [ ] **Step 1: Write API flow test**

The test must:

1. create an application
2. upload/import a LaTeX resume
3. render the resume
4. analyze the packet
5. fail approval with blocker
6. add evidence or use clean fixture
7. approve packet
8. mark submitted manually

Run: `python -m pytest tests/test_api_flow.py -q`

Expected: fail because API does not exist.

- [ ] **Step 2: Implement API**

Required endpoints:

- `GET /health`
- `GET /applications`
- `POST /applications`
- `GET /applications/{id}`
- `POST /applications/{id}/resume`
- `POST /resumes/{id}/render`
- `POST /applications/{id}/analyze`
- `POST /applications/{id}/approve`
- `POST /applications/{id}/mark-submitted`

- [ ] **Step 3: Run API tests**

Run: `python -m pytest tests/test_api_flow.py -q`

Expected: pass.

- [ ] **Step 4: Commit**

```powershell
git add apps/api tests/test_api_flow.py
git commit -m "feat: expose local application API"
```

## Task 7: React Application Desk UI

**Files:**
- Create: `apps/web/package.json`
- Create: `apps/web/index.html`
- Create: `apps/web/src/main.tsx`
- Create: `apps/web/src/App.tsx`
- Create: `apps/web/src/api.ts`
- Create: `apps/web/src/styles.css`
- Create: `apps/web/src/App.test.tsx`

- [ ] **Step 1: Write frontend tests**

Tests must prove:

- empty state says `No applications yet. Add a JD to start a packet.`
- `Add application` form validates required fields
- application list renders a created application
- status labels use approved wording
- no banned marketing copy appears

Run: `npm --prefix apps/web run test -- --run`

Expected: fail because app does not exist.

- [ ] **Step 2: Create Vite React app files**

Use React TypeScript without a complex component library. Use CSS tokens and semantic classes.

- [ ] **Step 3: Implement UI**

Screens:

- left application rail
- center JD and resume work area
- right checks and approval panel

Labels:

- `Add application`
- `Render PDF`
- `Analyze`
- `Approve packet`
- `Mark submitted manually`
- `Missing evidence`
- `PDF is stale`

- [ ] **Step 4: Run frontend tests**

Run: `npm --prefix apps/web run test -- --run`

Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add apps/web
git commit -m "feat: add application desk UI"
```

## Task 8: Playwright Day-1 Workflow

**Files:**
- Create: `playwright.config.ts`
- Create: `tests/e2e/application-desk.spec.ts`
- Create: `tests/e2e/fixtures.ts`

- [ ] **Step 1: Write Playwright test**

The test must:

1. start from empty state
2. create an application
3. import sample LaTeX
4. render PDF
5. analyze
6. see blocker
7. add evidence or use clean resume path
8. approve
9. mark submitted manually

Run: `npm run test:e2e`

Expected: fail before UI/API wiring is complete.

- [ ] **Step 2: Configure Playwright web servers**

Start:

- API at `http://127.0.0.1:8765`
- UI at `http://127.0.0.1:5173`

- [ ] **Step 3: Run E2E test**

Run: `npm run test:e2e`

Expected: pass.

- [ ] **Step 4: Add responsive assertions**

Repeat smoke checks at:

- 320
- 375
- 414
- 768
- 1280

Assert no horizontal scroll and primary buttons remain visible.

- [ ] **Step 5: Commit**

```powershell
git add playwright.config.ts tests/e2e
git commit -m "test: add day-one UI workflow"
```

## Task 9: Final Verification

**Files:**
- Modify: `README.md`
- Modify: docs only if behavior differs from plan

- [ ] **Step 1: Add run instructions**

Document:

- install commands
- API dev command
- UI dev command
- test commands
- private data locations

- [ ] **Step 2: Run full verification**

Run:

```powershell
python -m pytest -q
npm --prefix apps/web run test -- --run
npm run test:e2e
git status --short
```

Expected:

- backend tests pass
- frontend tests pass
- Playwright passes
- git status is clean after committing

- [ ] **Step 3: Commit docs**

```powershell
git add README.md docs
git commit -m "docs: add local run instructions"
```

## Self-Review Notes

- PRD v0 scope maps to Tasks 1 through 8.
- Day-1 workflow maps to Tasks 6 through 8.
- UI principles map to Task 7 and Playwright responsive checks.
- Quality gates map to Tasks 5, 6, and 8.
- Cloud watcher, job scraping, auto-submit, public deployment, and Resume Worded automation are deliberately excluded.
- The implementation must keep private runtime state under ignored `data/` and `vault/`.

