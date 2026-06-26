# Observability

OpenTelemetry from day one (§4.8): Django (+ psycopg, requests) is auto-instrumented
(`backend/config/otel.py`) and exports OTLP traces + metrics to the **existing local
Watchtower (LGTM) stack** — specifically the **Grafana Alloy** collector on the host
(`4317` gRPC / `4318` HTTP), which forwards to Tempo / Loki / Prometheus and is viewed
in `watchtower-grafana` (http://localhost:3000).

The backend container reaches the host collector via
`OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318` (set in
`docker-compose.yml`). It does **not** run its own collector or LGTM — reuse, don't
rebuild.

## Seeing traces
1. `make up` (or `docker compose up -d`), then exercise the app (`make smoke`, or the
   browsable API).
2. Open Grafana → **Explore** → **Tempo** → search service `watch-backend`. HTTP
   request spans (and nested psycopg spans) appear there.

## `otel-collector-config.yaml`
Reference only — a standalone Collector pipeline (masking/redaction processors, LGTM
exporters) for environments **without** Watchtower. Not used by the local stack,
which sends straight to the Watchtower Alloy. Masked drains and telemetry-quality
monitors (§4.8) live in the Watchtower Alloy/collector config in the real estate.
