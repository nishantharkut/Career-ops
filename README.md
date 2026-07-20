# CareerOps

Private application desk for India-focused internship applications. The v0 workflow creates an application packet from a JD, imports a LaTeX resume, renders a PDF locally, runs fit and risk checks, blocks unsafe approval, and records manual submission.

## Current Shape

- Backend: FastAPI, SQLite, local file vault.
- Frontend: React, TypeScript, Vite.
- Resume rendering: local LaTeX through `latexmk` or `pdflatex`.
- Resume parsing: local PDF text extraction.
- Hosting: local by default, hostable as one authenticated container.

No paid AI API, Resume Worded scraping, auto-apply, or cloud resume processing is in the v0 path.

## Install

From `C:\N Drive\Internships + Placements\CareerOps`:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm install
npm --prefix apps/web install
```

The LaTeX renderer needs `latexmk` or `pdflatex` on `PATH`.

## Local Development

Terminal 1:

```powershell
npm run dev:api
```

Terminal 2:

```powershell
npm run dev:web
```

Open:

```text
http://127.0.0.1:5173
```

## Production-Style Local Serving

This serves the built React app and API from one origin.

```powershell
npm run build:web
npm run serve:hosted
```

Open:

```text
http://127.0.0.1:8765
```

For hosted use, set:

```powershell
$env:CAREEROPS_REQUIRE_AUTH = "true"
$env:CAREEROPS_AUTH_USERNAME = "nishant"
$env:CAREEROPS_AUTH_PASSWORD = "<long-random-password>"
```

## Docker Hosting

Use this for a free VM or cheap VPS. It keeps app data in persistent host folders.

```powershell
docker compose up -d --build
```

Required environment values are shown in `.env.example`.

The hosted app listens on:

```text
http://<host>:8765
```

Use HTTPS through the VM provider, a reverse proxy, or a tunnel before entering private data over the public internet.

## Tests

```powershell
npm run test:api
npm run test:web
npm run build:web
npm run test:e2e
```

Full verification:

```powershell
npm run test:all
```

The Playwright suite starts an isolated API using:

- `data/e2e-careerops.db`
- `vault-e2e/`

## Private Data

Runtime private state is ignored by git:

- `data/`
- `vault/`
- `exports/`
- `.env`
- `vault-e2e/`

Do not expose the API publicly without auth and HTTPS. Do not use free hosted storage as the only source of truth for resumes, PDFs, approvals, or reports unless cloud storage is explicitly approved.

## Hosting Decision

See `docs/hosting-strategy.md`.
