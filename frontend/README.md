# Frontend (React SPA on S3 + CloudFront)

Lineage: `gotoplanb/hermit-watch-gen` (§4.4). Scaffold is intentionally thin — the
one piece worth pinning down now is **honest degradation** (ADR-005), shown in
`src/health.js`.

## Deploy (audit lives in AWS — ADR-004)
CodeBuild builds → S3 sync → CloudFront invalidation. Fingerprinted assets (long
TTL) + short-TTL `index.html`, so a deploy never serves a half-old/half-new bundle.

## Honest degradation (ADR-005)
The static shell staying up must never *imply* liveness it doesn't have. The SPA
polls `GET /api/health` (dependency-checked: Postgres + Valkey). When the backend is
unreachable or `degraded`, the SPA shows a loud read-only/stale banner and the
documented "use ServiceNow" fallback — see `src/health.js`.

## To build out
- Vite + React app shell, incident list/detail, ack/escalate/resolve actions.
- OTel browser instrumentation → Collector (§4.8, `smokeshow` lineage for E2E).
