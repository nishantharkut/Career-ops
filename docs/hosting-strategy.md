# CareerOps Hosting Strategy

Status: implemented v0 deployment boundary.

## Supported Shapes

CareerOps runs in two supported shapes:

1. Local Windows development or one-origin serving with an optional managed Windows Ollama process or ChatGPT-authenticated Codex.
2. Docker Compose on a user-controlled host with same-origin FastAPI and React, Basic Auth, persistent bind mounts, and managed Ollama.

Both shapes keep submission manual and user-approved. AI runs only where the selected provider is installed, authenticated, reachable, and available.

## Same-Origin Application

`npm run build:web` creates `apps/web/dist`. FastAPI serves the built React files and API from one origin. Compose exposes port `8765`; `/health` is public, while Basic Auth protects the UI and private API routes.

Compose requires these private `.env` values:

```text
CAREEROPS_AUTH_USERNAME=<private username>
CAREEROPS_AUTH_PASSWORD=<long random password>
```

`CAREEROPS_REQUIRE_AUTH=true` is set in Compose. Startup fails when credentials are absent. `CAREEROPS_ALLOWED_ORIGINS` is available for an explicitly separate frontend, but the shipped hosted shape does not need cross-origin access.

Basic Auth is not transport encryption. Put a TLS reverse proxy, a private HTTPS tunnel, or hosting-provider HTTPS in front of port `8765` before entering real profile, evidence, JD, or resume data over a network.

## Persistent State

The shipped Compose topology persists:

- `./data` at `/app/data` for SQLite
- `./vault` at `/app/vault` for JD, resume, report, and AI artifacts
- `./backups` at `/app/backups` for backup archives
- `ollama_models` at `/root/.ollama` for local model data

`storage-init` runs once with only `CHOWN`, no network, and the three bind mounts. It sets the configured `CAREEROPS_UID` and `CAREEROPS_GID` ownership before the non-root API container starts.

The API, Ollama runtime, runtime inspector, and Docker socket proxy use `restart: unless-stopped` and health checks. The API health check reaches local `/health`; Ollama health checks both `ollama list` and the configured model; the inspector verifies Docker access through the proxy.

Do not use ephemeral container files as the source of truth. Do not remove the named model volume unless a full model download is intended.

## Recommended Low-Cost Host

The approved v0 hosting decision is one self-contained Docker deployment on an Always Free or inexpensive VPS whose CPU, memory, disk, and bandwidth can support the selected Ollama model. FastAPI and React stay on one origin; SQLite, the vault, and backups use persistent host volumes; the Ollama model uses its persistent named volume. Copy checksummed CareerOps backups off the host on a schedule. If the host cannot run the selected model, AI is available only through another explicitly configured reachable provider; no paid or cloud provider is added implicitly.

These alternatives are not v0:

- Serverless containers or functions with ephemeral storage do not provide the stable SQLite, vault, model, or background-task process boundary this release uses.
- A hosted database or object store is not the primary source of truth. Splitting the implemented SQLite-plus-vault transaction and backup topology across managed services would require a different persistence design and can add recurring cost.
- A hosted page does not bridge to Ollama or another worker on the browser machine's `localhost`. That path is fragile across browser origin, HTTPS, firewall, process identity, and runtime-attestation boundaries.

## Managed Ollama Boundary

### Bootstrap

`ollama-bootstrap` uses the same digest-pinned Ollama image as the runtime. It is the only Ollama service attached to `model_bootstrap_egress`. On startup it:

1. Starts loopback-only Ollama with `OLLAMA_NO_CLOUD=1`.
2. Waits for the local service.
3. Checks the exact `CAREEROPS_OLLAMA_MODEL`.
4. Pulls it only when absent.
5. Verifies the model and exits.

The first pull can be large and slow. The persistent volume makes subsequent startup reuse the downloaded model. The bootstrap container has egress only for this installation step; the runtime container is not attached to that network.

### Runtime And Attestation

The runtime `ollama` service:

- sets `OLLAMA_NO_CLOUD=1`
- publishes no host port
- has a read-only root filesystem and dropped capabilities
- attaches only to the internal `ollama_private` bridge
- mounts the persistent model volume
- starts only after bootstrap succeeds

The Docker socket is not mounted into the API or inspector. A digest-pinned socket proxy exposes GET-only container, network, and version inspection. All mutation endpoints are disabled. `runtime-inspector` uses that proxy to attest exactly one Compose Ollama container, its full container ID, start identity, image reference and image ID, cloud-disabled environment, lack of host ports, single internal network, and private service address.

The API adopts only that attested record. Its in-process `OllamaRuntimeAuthority` signs a capability bound to the canonical endpoint, exact model, runtime instance, container handle, image identities, and no-egress policy. The signing key is generated in the API process and is not stored in Compose, source, SQLite, or the inspector.

Before availability and before every request, the provider verifies the signature and asks the inspector to reattest the exact runtime. It verifies local `/api/tags` and `/api/show` metadata, rejects remote/cloud metadata, sends a bounded schema request, and checks model metadata again after generation. Container, network, image, model, endpoint, capability, or metadata drift fails closed.

Only this verified path reports `local_private` and `data_leaves_machine=false`. A generic localhost daemon, external URL, environment assertion, or stale capability is unavailable and is never relabelled as private.

## Provider Reachability

Managed Ollama is included in the Compose topology. It requires no paid API and sends task data only to the verified internal runtime.

Codex uses a ChatGPT-authenticated CLI and is always `cloud_external`. The standard Linux image does not install Codex, persist a Codex home, or authenticate an account. Setting `CAREEROPS_CODEX_ENABLED=true` alone normally discovers no Codex provider in hosted Linux. A custom deployment must explicitly install the native CLI, provide the supported identity descriptor, authenticate the intended `CODEX_HOME`, and persist that home outside the read-only image. Each task still requires the UI acknowledgement and sends bounded task data to OpenAI.

The deterministic verifier is always local, available, and not AI. It keeps safety checks usable when an AI provider is unavailable, but it does not claim semantic model review and cannot authorize approval or submission without an AI-backed `analyze_fit` report.

There is no OpenAI API adapter, bundled API key, paid scoring service, remote resume analyzer, or hidden cloud AI fallback in v0.

## Start And Observe

From the project root:

```powershell
Copy-Item -LiteralPath .env.example -Destination .env
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs careerops ollama-bootstrap ollama runtime-inspector docker-socket-proxy
```

Open `http://<host>:8765` only through the configured HTTPS boundary for real data. A healthy deployment has a `200` response from `/health`, a `401` response from private routes without Basic Auth, and a successful authenticated UI load.

## Hosted Backup

The checksummed backup includes SQLite and the vault. It does not include the model volume because bootstrap can repopulate that volume.

Use a quiet maintenance window. A conservative host sequence is:

```powershell
Set-Location -LiteralPath "C:\N Drive\Internships + Placements\CareerOps"
docker compose stop careerops
.\scripts\backup.ps1 `
  -BackupRoot "C:\CareerOpsBackups" `
  -DataPath "C:\N Drive\Internships + Placements\CareerOps\data" `
  -VaultPath "C:\N Drive\Internships + Placements\CareerOps\vault"
docker compose start careerops
```

Copy the published ZIP and its printed archive SHA-256 to separate storage. Do not copy the live SQLite files manually. If any vault or database-bound artifact changes during snapshotting, the backup command fails instead of publishing a mixed archive.

Restore to separate empty paths, verify the restored application, then switch deployment mounts in a controlled maintenance window. The restore command never overwrites the active `data` or `vault` directories.

## Failure Handling

- **Bootstrap still running:** Follow `docker compose logs -f ollama-bootstrap`; a first model pull can take time and disk space.
- **Bootstrap failed:** Check network/DNS and free disk space. Correct `CAREEROPS_OLLAMA_MODEL`, then rerun `docker compose up -d`.
- **Managed runtime unavailable:** Inspect `careerops`, `ollama`, `runtime-inspector`, and `docker-socket-proxy` logs. Any attestation mismatch intentionally removes the provider.
- **Runtime drift:** Run `docker compose up -d --build --force-recreate`. Do not add `-v` when the model volume should survive.
- **Auth startup failure:** Set both auth values in the private `.env`; do not commit it.
- **Bind-mount permission failure:** Inspect `storage-init`, confirm host directories are regular paths, and set host-compatible `CAREEROPS_UID` and `CAREEROPS_GID`.
- **Health failure:** Check SQLite path permissions, bind-mount availability, model health, and dependency container state.
- **Codex unavailable in Linux:** This is expected for the shipped image. Use managed Ollama or build and authenticate an explicit Codex-enabled image; never assume the host's Codex login exists inside the container.

## Deferred Boundaries

Cloud job watching, public job discovery, email ingestion, automatic finding application, browser form filling, and automatic submission are not implemented. Any later public watcher must remain separate from private profile, resume, evidence, report, credentials, and application notes.
