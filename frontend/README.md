# Frontend — status page (React SPA)

Per **ADR-011**, the React SPA is the **read-only status page** (system health +
incident posture). The interactive working surface lives in Django (`/ui/...`).
Lineage: `gotoplanb/hermit-watch-gen` (§4.4).

## Build-less (local)
React + `htm` load from an **ESM CDN** (`esm.sh`) — no npm/Vite build (which this
sandbox can't run). `src/app.js` polls `GET /api/status` every 10s and renders:

- **Honest degradation banner (ADR-005):** green (ok) / amber (a dependency degraded) /
  red "use ServiceNow" (backend unreachable). The static shell staying up never implies
  liveness it doesn't have.
- **Posture cards:** open incidents, open-by-tier (T1/T2/T3), resolved in the last 24h.

`/api/status` is public + CORS-open (aggregate counts only), so the SPA can call it from
its own origin.

## Run it
```bash
make status-page          # serves this dir at http://localhost:5173
# open http://localhost:5173  (expects the API at http://localhost:8010)
```
Point at a different API by editing `window.WATCH_API` in `index.html`.

## Production (deferred)
Compile to a **fingerprinted bundle** (long-TTL assets + short-TTL `index.html`) on
S3 + CloudFront via CodeBuild (ADR-005 / §4.6) — replaces the ESM-CDN imports. OTel
browser instrumentation + SmokeShow/Playwright E2E (§4.8) are the next layers.
