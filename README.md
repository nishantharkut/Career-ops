# CareerOps

Status: approved and implemented Application Desk v0.

CareerOps is a private, single-user application desk for India-focused internship applications. It stores a job description, evidence, LaTeX resume versions, AI task history, reports, approvals, and manual submission history. It does not apply to jobs or submit forms.

## Implemented Workflow

- FastAPI, SQLite, and a checksummed file vault hold the durable state.
- React and FastAPI run from one origin in production-style and Compose modes.
- `latexmk` or `pdflatex` renders a selected `.tex` resume locally.
- Five structured tasks are available: `analyze_jd`, `analyze_fit`, `suggest_resume_edits`, `draft_cover_letter`, and `review_claims`.
- Providers are ChatGPT-authenticated Codex, verified managed Ollama, and a deterministic verifier that is explicitly not AI.
- Every task is bound to one provider configuration, model, privacy mode, prompt ID, prompt checksum, and provider provenance snapshot.
- Accepted resume edits create a child resume version. The parent source is unchanged.
- Approval is user-controlled and binds the current application, resume version, report, and PDF checksum. Submission is recorded only after the user submits outside CareerOps.

See [docs/day-1-workflow.md](docs/day-1-workflow.md) for the exact UI sequence.

## Install

Run from `C:\N Drive\Internships + Placements\CareerOps`:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm install
npm --prefix apps/web install
```

Install `latexmk` or `pdflatex` on `PATH` for resume rendering.

## Local Development

Terminal 1:

```powershell
npm run dev:api
```

Terminal 2:

```powershell
npm run dev:web
```

Open `http://127.0.0.1:5173`.

For one-origin local serving:

```powershell
npm run build:web
npm run serve:hosted
```

Open `http://127.0.0.1:8765`.

## Codex Provider

Enable the two separate ChatGPT identities before starting the API:

```powershell
$env:CAREEROPS_CODEX_ENABLED = "true"
$env:CAREEROPS_CODEX_COMMANDS = "codex2,codex"
npm run dev:api
```

`codex2` is preferred when both identities pass discovery. `codex2` and `codex` are separate provider records with separate `CODEX_HOME` directories. A task stays bound to the selected identity; a failed `codex2` task is never retried through `codex`.

The approved `codex2.cmd` descriptor on `PATH` is exactly:

```bat
@echo off
set "CODEX_HOME=%USERPROFILE%\codex-account2\.codex"
codex %*
exit /b %ERRORLEVEL%
```

The native executable remains `codex.exe`; CareerOps validates and hashes it, validates `codex-cli` version output, checks `codex login status`, and invokes the native executable directly with an argument list. The default implemented model is `gpt-5.6-sol` with `xhigh` reasoning effort. `CAREEROPS_CODEX_MODEL` and `CAREEROPS_CODEX_REASONING_EFFORT` override those values.

Codex is `cloud_external`: the bounded selected task prompt and context leave the machine through the selected signed-in ChatGPT account. The UI disables the run command until `Acknowledge Codex data transfer for this run` is checked. The acknowledgement resets after a launch or provider/task change. No API key or account secret is stored in source.

The provider label identifies `Codex account 2 (codex2)` or `Codex default (codex)`. The task panel shows the selected provider identity, model, privacy mode, task ID, prompt ID, and prompt checksum. Provider output and invocation provenance are checksummed in the task vault.

Availability checks:

```powershell
Get-Command codex2,codex
codex2 --version
codex2 login status
codex --version
codex login status
```

Run `codex2 login` or `codex login` for the identity that is unavailable, then restart the API. CareerOps rejects an altered `codex2.cmd`, a non-native executable override, a missing `CODEX_HOME`, an unverifiable version, or a non-ChatGPT login.

## Managed Ollama With Compose

Copy `.env.example` to a private `.env`, set both Basic Auth values, then start the stack:

```powershell
Copy-Item -LiteralPath .env.example -Destination .env
docker compose up -d --build
docker compose ps
```

Compose starts `ollama-bootstrap` on a temporary bootstrap-only egress network. It downloads the configured `CAREEROPS_OLLAMA_MODEL` only when that exact model is absent from the persistent `ollama_models` volume. The first download can be large and slow; later starts reuse the volume. Follow it with:

```powershell
docker compose logs -f ollama-bootstrap
```

The runtime `ollama` container has `OLLAMA_NO_CLOUD=1`, no published host port, and only the internal `ollama_private` network. `storage-init` prepares the `data`, `vault`, and `backups` bind mounts. A GET-only Docker socket proxy and `runtime-inspector` attest the exact running container, internal network, pinned image reference, image ID, and model setting. The API process then issues an authority-signed capability bound to that runtime and model. The provider reverifies that binding and local model metadata before requests and checks model provenance again after generation. Drift fails closed.

A generic service at `localhost:11434` has no such capability. It remains unavailable and never receives the `local_private` label.

## Managed Ollama On Windows

Windows process mode does not download a model. The exact model must already exist under `<runtime root>\models`. Use an elevated PowerShell session because the launcher creates and verifies a program-specific outbound firewall rule.

```powershell
$env:CAREEROPS_OLLAMA_MANAGED_MODE = "windows_process"
$env:CAREEROPS_OLLAMA_URL = "http://127.0.0.1:11434"
$env:CAREEROPS_OLLAMA_MODEL = "qwen2.5:7b"
$env:CAREEROPS_OLLAMA_EXECUTABLE = "C:\Program Files\Ollama\ollama.exe"
$env:CAREEROPS_OLLAMA_RUNTIME_ROOT = "C:\N Drive\Internships + Placements\CareerOps\data\.managed-ollama"
npm run build:web
npm run serve:hosted
```

CareerOps copies the configured executable to `<runtime root>\<runtime instance>\bin\ollama.exe`. The Python-to-PowerShell launcher copies only this host allowlist: `SystemRoot`, `WINDIR`, `COMSPEC`, `PATHEXT`, `PATH`, `PSModulePath`, `TEMP`, and `TMP`. It sets `NO_PROXY` and `no_proxy` itself for loopback access; those values are not inherited.

PowerShell clears the final Ollama child environment. Its base set is only the available `PATH`, `SystemRoot`, `WINDIR`, `TEMP`, and `TMP` values, plus `OLLAMA_HOST`, `OLLAMA_NO_CLOUD`, `OLLAMA_MODELS`, `NO_PROXY`, and `no_proxy`. Application auth, API keys, Codex state or tokens, and unrelated environment variables are excluded from both stages. CareerOps then starts `ollama.exe serve`, verifies the executable hash, process start identity, PID, loopback listener, firewall rule, and exact installed model, and signs the capability in the API process.

On clean API shutdown, CareerOps verifies and stops that exact process, verifies that the listener is gone, removes its firewall rule, and removes the launch record. The model directory and instance files remain. Failed launches run journal recovery; cleanup refuses to stop a process or remove a rule whose recorded identity no longer matches.

## Verifier-Only Provider

`Deterministic verifier` is always registered. It is a useful local safety fallback for structured checks and human-review decisions. Its records use `verifier_only`, `data_leaves_machine=false`, and `is_ai=false`. The UI command is `Run verifier`; it is never presented as an AI review. Verifier-only output cannot authorize approval or submission. Approval requires the current report to come from a completed AI-backed `analyze_fit` task.

## Hosted Boundary

Compose serves React and FastAPI from the same origin on port `8765`. Basic Auth is required for every private route; `/health` remains public for health checks. The app, Ollama, inspector, and socket proxy use restart and health policies. Host bind mounts persist `data`, `vault`, and `backups`; the named model volume persists Ollama data.

Add HTTPS with a reverse proxy, tunnel, or hosting-provider TLS before entering real data over a network. Basic Auth without HTTPS does not protect credentials in transit.

AI runs only where its provider is reachable. The standard Linux image includes managed Ollama but does not install or authenticate Codex. Codex inside hosted Linux is therefore normally unavailable unless a custom image explicitly installs the CLI and provides a persistent authenticated `CODEX_HOME`; it remains `cloud_external` in that case. CareerOps has no hidden paid API or cloud AI dependency.

See [docs/hosting-strategy.md](docs/hosting-strategy.md) for the full boundary.

## Backup And Restore

Create a stable SQLite and vault backup with explicit paths:

```powershell
Set-Location -LiteralPath "C:\N Drive\Internships + Placements\CareerOps"
.\scripts\backup.ps1 `
  -BackupRoot "C:\CareerOpsBackups" `
  -DataPath "C:\N Drive\Internships + Placements\CareerOps\data" `
  -VaultPath "C:\N Drive\Internships + Placements\CareerOps\vault"
```

The command prints JSON containing the archive path, manifest checksum, archive checksum, file count, and byte count. It uses SQLite's online backup API, stages vault files, validates database-to-vault bindings, rechecks source identity and bytes, writes a bounded checksummed ZIP, and publishes it atomically. Concurrent source or staged-file mismatch fails the backup and leaves no published archive.

Restore only to new, existing, empty directories:

```powershell
New-Item -ItemType Directory -Path "C:\CareerOpsRestore"
New-Item -ItemType Directory -Path "C:\CareerOpsRestore\data"
New-Item -ItemType Directory -Path "C:\CareerOpsRestore\vault"
Set-Location -LiteralPath "C:\N Drive\Internships + Placements\CareerOps"
.\scripts\restore.ps1 `
  -ArchivePath "C:\CareerOpsBackups\careerops-backup-YYYYMMDDTHHMMSSZ-ARCHIVEID.zip" `
  -DataPath "C:\CareerOpsRestore\data" `
  -VaultPath "C:\CareerOpsRestore\vault"
```

Restore validates archive limits, member paths, manifest and file checksums, SQLite integrity, and staged topology before publication. A durable transaction journal recovers or rolls back an interrupted two-target publish. It never overwrites a non-empty target and refuses changed targets, links, reparse points, archive swaps, or mismatched recovery journals.

For a hosted instance, take the backup during a quiet window. For the clearest boundary, run `docker compose stop careerops`, run the host backup command against the mounted `data` and `vault` paths, copy the resulting archive off the VM, then run `docker compose start careerops`. Do not copy `careerops.db`, `-wal`, and `-shm` files manually. The Ollama model volume is not in the CareerOps backup and can be populated again by the bootstrap service.

## Tests

```powershell
npm run test:api
npm run test:web
npm run build:web
npm run test:e2e
```

Full normal verification excludes live providers:

```powershell
npm run test:all
```

Selecting the registered marker is the live-provider opt-in:

```powershell
.\.venv\Scripts\python.exe -m pytest -m live_provider -q
```

The Codex smoke discovers only `codex2`, uses synthetic JD data in `tmp_path`, and never falls back. The managed Ollama smoke skips unless the current environment explicitly configures a verified managed runtime fixture; generic localhost is not accepted.

## Troubleshooting

- **Provider unavailable:** Read the exact provider state in the AI panel. Restart the API after changing provider environment variables. The task remains bound to the identity selected at launch.
- **Codex wrapper or login:** Run the availability commands above. Restore the exact `codex2.cmd` descriptor, ensure `%USERPROFILE%\codex-account2\.codex` exists, and run `codex2 login`. Do not put credentials in the wrapper or repository.
- **Malformed provider output:** The task ends with `provider_error` or `invalid_output`; no finding is applied. Check the selected provider and rerun a new task after fixing it.
- **Provider timeout:** The task ends with `provider_timeout`. Confirm the provider is responsive, reduce external load, and launch a new task. CareerOps does not switch identities.
- **Managed Ollama bootstrap:** Use `docker compose logs ollama-bootstrap`. Leave the stack running while a first large pull completes. Do not use `docker compose down -v` unless the model volume is intentionally being deleted.
- **Managed Ollama unavailable or drifted:** Use `docker compose logs careerops ollama runtime-inspector docker-socket-proxy` and `docker compose ps`. Recreate the stack with `docker compose up -d --build --force-recreate`; the model volume remains unless `-v` is used. Runtime, image, network, or model mismatch removes `local_private` availability.
- **Windows managed Ollama:** Confirm the configured executable and runtime root are regular local paths, the exact model exists under `<runtime root>\models`, port `11434` is free, and the elevated shell can manage Windows Firewall. Startup and recovery fail closed when identity is ambiguous.
- **LaTeX failure:** Run `Get-Command latexmk,pdflatex`. The render log is stored under `vault\resumes\<resume id>\render\render.log`. Fix the original source outside CareerOps, upload it as a new resume version, and render that version.
- **Stale PDF:** A changed vaulted-source checksum makes the current PDF/report unsafe. Restore the vaulted file to its recorded bytes or upload the current source as a new resume version, then render and rerun; do not approve or export the stale report.
- **Interrupted task:** Reload the packet. Pending work is polled again; expired running claims are requeued. An invocation already fenced against replay may finish as `provider_error` instead of calling the provider twice.
- **Backup refusal:** Stop concurrent writes and rerun. Source mutation, a backup root inside the vault, links, permission failures, or archive limits are hard failures.
- **Restore refusal:** Use two separate existing empty directories and the same archive. Do not remove transaction journals or staging paths by hand; rerun the same restore so journal recovery can verify them.
- **Auth or permissions:** Compose requires non-empty `CAREEROPS_AUTH_USERNAME` and `CAREEROPS_AUTH_PASSWORD`. A `401` on private routes is expected without Basic Auth. Check `docker compose logs storage-init careerops`, `CAREEROPS_UID`, `CAREEROPS_GID`, and host bind-mount permissions. `/health` should remain `200` without auth.

## Private State And Scope

Keep `data/`, `vault/`, `backups/`, `.env`, and generated provider artifacts out of source control. Do not expose the API without Basic Auth and HTTPS.

Cloud job watching, automatic application discovery, auto-apply, automatic finding acceptance, and browser submission are deferred and out of scope for v0.
