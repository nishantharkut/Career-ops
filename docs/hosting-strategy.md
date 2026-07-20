# CareerOps Hosting Strategy

## Decision

CareerOps must support two deployment shapes:

1. **Private local mode** for day-to-day resume processing.
2. **Hostable self-contained mode** for a low/no-cost VM or container host.

The hosted v0 serves the React app and FastAPI API from the same origin, requires Basic Auth for private routes, uses SQLite plus file-vault persistent volumes, and exposes `/health` for uptime checks. It must not depend on paid AI APIs, hosted resume analysis, or cloud-only storage.

## Recommended Low/No-Cost Path

The recommended architecture is:

- `Dockerfile` builds the React app and FastAPI API into one container.
- `compose.yaml` runs the app on port `8765`.
- `./data` and `./vault` are mounted as persistent host volumes.
- `restart: unless-stopped` restarts the service after process or VM restarts.
- `/health` verifies the API can reach SQLite.
- `CAREEROPS_AUTH_USERNAME` and `CAREEROPS_AUTH_PASSWORD` are required for public hosted use.

This fits an Always Free VM or any cheap VPS. It is more reliable than a free serverless app with ephemeral storage because resume PDFs, LaTeX files, approval records, and SQLite data live on persistent volumes.

## Options Compared

### Option A: Free Static Host + Local Worker

Use Cloudflare Pages or a similar static host for the UI and keep private processing local.

Pros:

- Lowest cost.
- Static UI has high availability.
- Private resume files stay off third-party servers.

Cons:

- Browser-to-localhost access from a hosted HTTPS page can be fragile.
- The local worker still has to run when processing is needed.
- Not enough for true always-available use.

Use this later only if we build a robust local bridge and browser security checks.

### Option B: Self-Contained Docker App On Free/Cheap VM

Run the app container on a VM with persistent mounted volumes.

Pros:

- One URL for UI and API.
- No CORS or browser-localhost problem.
- Local open-source renderer and analyzer still work inside the container.
- Restart policy and health checks support uptime.
- Can be no-cost on an Always Free VM if capacity is available.

Cons:

- Needs VM setup and basic security hardening.
- Free VM capacity and idle policies are provider-dependent.
- Public access must use auth and HTTPS before real personal data is entered.

This is the v0 hosting target.

### Option C: Cloud Run Or Similar Serverless Container

Run the container on a scale-to-zero service.

Pros:

- Managed uptime and scaling.
- Low cost for light usage.
- No VM maintenance.

Cons:

- A billing account can be required for free-tier usage.
- Container filesystem is not a durable application database.
- Durable storage needs an external database or object store, which moves private resume data into cloud systems.
- LaTeX containers are large and can have cold starts.

Use this only after explicit approval for cloud storage and a production storage adapter.

### Option D: Supabase Or Hosted Database First

Use a hosted database and storage service as the primary backend.

Pros:

- Fast to build shared access and sync.
- Free tier is useful for prototypes.

Cons:

- Free projects can pause after inactivity.
- Private resume artifacts would move to cloud storage.
- Not compatible with the current local-private-processing requirement without client-side encryption and a local worker.

Not the v0 target.

## Current Implementation Requirements

The codebase supports:

- Same-origin production serving from `apps/web/dist`.
- `VITE_CAREEROPS_API` override for separate API hosting.
- Localhost API default only during Vite development.
- Configurable `CAREEROPS_ALLOWED_ORIGINS`.
- Optional Basic Auth via `CAREEROPS_AUTH_USERNAME` and `CAREEROPS_AUTH_PASSWORD`.
- Fail-fast auth when `CAREEROPS_REQUIRE_AUTH=true` but credentials are missing.
- SQLite WAL mode, `synchronous=FULL`, and busy timeout.
- Container health checks.

## Security Rules

- Do not expose the API publicly without auth.
- Do not put real resume data in a cloud database or object store without explicit user approval.
- Do not use a free database with inactivity pauses as the only production source of truth.
- Do not rely on ephemeral container storage for applications, resumes, reports, or approvals.
- Add HTTPS through the host or reverse proxy before entering private data on a public URL.

## Sources Checked

- Cloudflare Pages free limits: https://developers.cloudflare.com/pages/platform/limits/
- Cloudflare Pages pricing page: https://pages.cloudflare.com/
- Google Cloud Run pricing/free tier: https://cloud.google.com/run/pricing
- Google Cloud free tier billing requirement: https://docs.cloud.google.com/free/docs/free-cloud-features
- Supabase pricing/free-tier pause behavior: https://supabase.com/pricing
- Oracle Always Free resources and idle reclaim policy: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
