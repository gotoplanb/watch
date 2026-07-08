# Watch — local developer workflow.
# Goal: everything runs local — unit + integration tests, manual use, and OTel
# telemetry visible in the local Watchtower (LGTM) stack.
#
# `make help` is auto-generated: any target with a `## description` after its
# colon shows up in the list. Add the comment when you add a target — that's the
# only maintenance the help needs (no hand-curated echo list to drift).

PY := python3.12
VENV := backend/.venv
PYTEST := $(VENV)/bin/pytest

.PHONY: help venv test e2e demo dev infra status-page up down logs seed seed-dev smoke \
        coverage sonar-scan sonar-scan-only install-hooks integration clean \
        tunnel-domain tunnel-up tunnel-down tunnel-status

# Env for running the backend on the HOST against compose-provided infra. This is the
# primary local loop here: it needs no image-registry/PyPI egress (only the cached
# postgres/valkey/appconfig images), and exports OTel to the existing Watchtower.
comma := ,
# Tunnel host from .env (TUNNEL_DOMAIN), if set — lets the ngrok dev tunnel's Host pass Django's
# ALLOWED_HOSTS / CSRF checks. Empty when no tunnel is configured (no effect on the plain loop).
TUNNEL_HOST := $(shell test -f .env && sed -n 's/^TUNNEL_DOMAIN=//p' .env | tr -d '"' | head -1)
# Paging topic secret from .env (ADR-013) — salts ntfy topic names so they're not guessable from the
# public source. Empty when unset (plain topics). Kept in gitignored .env, never in this file.
NTFY_TOPIC_SECRET := $(shell test -f .env && sed -n 's/^NTFY_TOPIC_SECRET=//p' .env | tr -d '"' | head -1)
# Seeded tier-user password from .env (ADR-008) — the e2e smoke logs in as a tier user, so it must
# match whatever seed_demo applied. Empty here → the e2e recipe falls back to the dev default 'watch'.
SEED_USER_PASSWORD := $(shell test -f .env && sed -n 's/^SEED_USER_PASSWORD=//p' .env | tr -d '"' | head -1)

HOSTENV := DJANGO_SECRET_KEY=dev DJANGO_DEBUG=1 \
  $(if $(NTFY_TOPIC_SECRET),NTFY_TOPIC_SECRET=$(NTFY_TOPIC_SECRET)) \
  DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,host.docker.internal$(if $(TUNNEL_HOST),$(comma)$(TUNNEL_HOST)) \
  $(if $(TUNNEL_HOST),CSRF_TRUSTED_ORIGINS=https://$(TUNNEL_HOST)) \
  POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
  VALKEY_URL=redis://localhost:6380/0 \
  APPCONFIG_AGENT_URL=http://localhost:2772 FLAGS_PROVIDER=appconfig \
  INTAKE_WEBHOOK_SECRET=dev-webhook-secret CHECKS_WEBHOOK_SECRET=dev-webhook-secret \
  WEBHOOK_ECHO_SECRET=dev-echo-secret SESSION_USER_HMAC_KEY=dev-hmac-key API_KEY_SECRET=dev-api-key-secret \
  OTEL_ENABLED=1 OTEL_SERVICE_NAME=watch-backend OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

# Seed the DB applying .env's SEED_USER_PASSWORD / SEED_ADMIN_PASSWORD. settings read os.environ only
# and HOSTENV doesn't carry the seed passwords, so we source .env first (then $(HOSTENV) overrides the
# infra vars back to localhost). Shared by `dev` and `seed-dev` so a `make dev` restart keeps rotated
# creds instead of silently resetting them to the watch/admin defaults.
SEED_CMD = cd backend && { set -a; test -f ../.env && . ../.env; set +a; } && $(HOSTENV) .venv/bin/python manage.py seed_demo

help: ## Show this list (any target with a trailing comment)
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'
	@echo ""
	@echo "Watchtower Grafana (existing): http://localhost:3000  -> Explore -> Tempo (service watch-backend)"
	@echo "App / browsable API:           http://localhost:8010/api/  (login t1a..t3b / admin; pw from SEED_USER_PASSWORD/SEED_ADMIN_PASSWORD in .env)"

venv:
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -r backend/requirements.txt

infra: ## Start only Postgres/Valkey/AppConfig in Docker
	@test -f .env || cp .env.example .env
	docker compose up -d postgres valkey appconfig-agent

# Serve the build-less React status page (ADR-011). Expects the API on :8010.
status-page: ## Serve the build-less React status page on :5173 (API on :8010)
	@echo "status page -> http://localhost:5173  (API at http://localhost:8010)"
	cd frontend && python3 -m http.server 5173

# One-command working loop (no image build): infra in Docker, app on the host.
dev: venv infra ## PRIMARY local loop — infra in Docker + backend on host (:8010): migrate+seed+runserver
	@echo "Waiting for Postgres on :5433..."
	@for i in $$(seq 1 30); do nc -z localhost 5433 && break; sleep 1; done
	cd backend && $(HOSTENV) .venv/bin/python manage.py migrate
	$(SEED_CMD)
	cd backend && $(HOSTENV) .venv/bin/python manage.py runserver --noreload 0.0.0.0:8010

test: venv ## Run hermetic unit tests (no Docker)
	cd backend && .venv/bin/pytest

# Post-deploy functional smoke (Playwright). Defaults hit the `make dev` loop; override
# E2E_BASE / E2E_STATUS / E2E_SECRET to run against staging (the pipeline Smoke stage does).
E2E_BASE ?= http://localhost:8010
E2E_STATUS ?= http://localhost:5173
E2E_SECRET ?= dev-webhook-secret
# Login the smoke uses (must match the seeded DB). Password from .env's SEED_USER_PASSWORD, else 'watch'.
E2E_USER ?= t1a
E2E_PASSWORD ?= $(or $(SEED_USER_PASSWORD),watch)
# Suite tiering (#30): local runs skip @staging-only tests; the staging Smoke stage overrides
# E2E_GREP="" (or --grep="@local|@staging") to run the full superset.
E2E_GREP ?= --grep-invert=@staging
# Browser provisioning is OPT-IN. Default 0 = never run `npx playwright install`, because on some
# macOS setups its unzip hangs forever (download finishes, extraction freezes). Local dev seeds
# browsers out-of-band with `ditto` (see e2e/README.md), so the installer is never needed and a
# plain `make e2e` / `git commit` can't hang. Opt in with E2E_INSTALL=1 only where the installer
# actually works (Linux, or a Mac you've verified). CI is unaffected — the pipeline Smoke stage
# (platform modules/pipeline/smoke.tf) uses the prebuilt playwright image and never calls this.
E2E_INSTALL ?= 0

# Record release demo videos from the versioned storyboards (shot-scraper video -> webm + mp4).
# Needs the local stack up (make dev + make status-page) and shot-scraper installed
# (`uv tool install shot-scraper`; seed its Playwright browser out-of-band — see storyboards/README.md).
DEMO_OUT ?= /tmp/watch-demos
demo: ## Record release demo videos from storyboards (shot-scraper; needs the stack up)
	@command -v shot-scraper >/dev/null || { echo "shot-scraper not installed (uv tool install shot-scraper); see storyboards/README.md"; exit 1; }
	@mkdir -p $(DEMO_OUT)
	@for s in storyboards/*.yml; do \
	  echo "recording $$s"; \
	  shot-scraper video $$s -o $(DEMO_OUT)/$$(basename $$s .yml).webm --mp4 || exit 1; \
	done
	@echo "demos -> $(DEMO_OUT)"

e2e: ## Playwright post-deploy smoke (defaults to the make-dev loop; override E2E_* for staging)
	@cd e2e && npm install --silent \
	  && { [ "$(E2E_INSTALL)" != 1 ] || npx playwright install chromium chromium-headless-shell; } \
	  && SMOKE_USER=$(E2E_USER) SMOKE_PASSWORD='$(E2E_PASSWORD)' \
	     BASE_URL=$(E2E_BASE) STATUS_URL=$(E2E_STATUS) INTAKE_WEBHOOK_SECRET=$(E2E_SECRET) npx playwright test $(E2E_GREP)

# Run hermetic units under coverage; writes backend/coverage.xml (Cobertura) and a
# terminal summary. Fails if total coverage < 90% (the gate, in pyproject.toml).
coverage: venv ## Run units under coverage; fails if < 90% (the gate)
	cd backend && .venv/bin/pytest --cov --cov-report=xml --cov-report=term -q

# Static analysis + coverage to the local Watchtower SonarQube (:9000). Reads
# SONAR_TOKEN from .env. Runs the scanner with backend/ as the base dir and waits for
# the quality gate (sonar.qualitygate.wait in sonar-project.properties), so a red gate
# exits non-zero. Results at http://localhost:9000/dashboard?id=watch
sonar-scan: coverage sonar-scan-only ## Coverage + SonarQube scan (local Watchtower :9000, project watch)

# Scan using the already-generated backend/coverage.xml (used by the pre-commit hook,
# which runs coverage itself first).
sonar-scan-only:
	@if [ -z "$$SONAR_TOKEN" ] && ! grep -q '^SONAR_TOKEN=' .env 2>/dev/null; then \
		echo "Set SONAR_TOKEN in .env (generate at http://localhost:9000)"; exit 1; \
	fi
	@set -a; . ./.env; set +a; \
	cd backend && docker run --rm \
		-e SONAR_HOST_URL=http://host.docker.internal:9000 \
		-e SONAR_TOKEN=$$SONAR_TOKEN \
		-v "$$(pwd):/usr/src" \
		sonarsource/sonar-scanner-cli:latest

# Install the versioned git hooks (coverage + Sonar gates on commit).
install-hooks: ## Install the pre-commit quality gates (coverage + Sonar)
	git config core.hooksPath .githooks
	@chmod +x .githooks/* 2>/dev/null || true
	@echo "git hooks installed (core.hooksPath=.githooks)"

up: ## Full containerized stack (needs registry/PyPI egress; for CI / normal net)
	@test -f .env || cp .env.example .env
	docker compose up -d --build
	@echo "Stack up. App http://localhost:8010/api/  •  traces in Watchtower Grafana http://localhost:3000"

logs: ## Tail the backend container logs (make up)
	docker compose logs -f backend

seed: ## Reseed the containerized (make up) DB
	docker compose exec backend python manage.py seed_demo

# Dev peer of `make seed`. `make seed` execs the *containerized* backend (make up); the make-dev loop
# runs the backend on the HOST venv with no backend container, so `docker compose exec backend` fails
# ("service backend is not running"). This reseeds the host-venv DB and, crucially, sources .env so a
# rotated SEED_USER_PASSWORD / SEED_ADMIN_PASSWORD actually applies (settings read os.environ only, and
# HOSTENV doesn't carry them). $(HOSTENV) after the source overrides infra vars back to localhost.
seed-dev: venv infra ## Reseed the make-dev (host-venv) DB, applying SEED_USER/ADMIN_PASSWORD from .env
	@echo "Waiting for Postgres on :5433..."
	@for i in $$(seq 1 30); do nc -z localhost 5433 && break; sleep 1; done
	$(SEED_CMD)

smoke: ## Push an incident through the intake webhook
	curl -fsS -X POST http://localhost:8010/api/intake/webhook \
	  -H "X-Watch-Webhook-Secret: $$(grep INTAKE_WEBHOOK_SECRET .env | cut -d= -f2)" \
	  -H "Content-Type: application/json" \
	  -d '{"source":"sumo","title":"Smoke: cpu high on web-2","source_event_id":"smoke-1","payload":{"host":"web-2"}}' \
	  && echo "" && echo "OK — see it at http://localhost:8010/api/incidents/"

# Integration tests (spec §6): real Postgres + AppConfig Agent + Step Functions Local.
# Tests run on the host venv against the running infra; SFN Local is best-effort
# (the test skips if its container can't start, e.g. no registry egress).
integration: venv infra ## Integration tests vs running infra (Postgres, AppConfig, [SFN if up])
	-docker compose --profile integration up -d stepfunctions-local
	@echo "Giving Step Functions Local a moment (skipped if unavailable)..."
	@for i in $$(seq 1 15); do nc -z localhost 8083 && break; sleep 1; done; true
	cd backend && DJANGO_SETTINGS_MODULE=config.settings_integration \
	  POSTGRES_HOST=localhost POSTGRES_PORT=5433 VALKEY_URL=redis://localhost:6380/0 \
	  APPCONFIG_AGENT_URL=http://localhost:2772 \
	  .venv/bin/pytest -m integration -p no:cacheprovider

down: ## Stop the stack and remove volumes
	docker compose --profile integration down -v

clean: down ## down + remove the venv
	rm -rf $(VENV)

# --- dev tunnels (ngrok): on-demand basic-auth'd public ingress (local/tunnel) ---
# Two tunnels: watch -> :8010 (make dev), status -> :5173 (make status-page). Scope any target with
# TUN=watch|status|both (default both), e.g. `make tunnel-up TUN=status`.
TUN ?= both
tunnel-domain: ## Reserve ngrok tunnel domain(s) from .env (Terraform; needs NGROK_API_KEY). TUN=watch|status|both
	local/tunnel/tunnel.sh domain $(TUN)
tunnel-up: ## Start ngrok tunnel(s) (basic-auth); prints the URL(s). TUN=watch|status|both
	local/tunnel/tunnel.sh up $(TUN)
tunnel-down: ## Stop ngrok tunnel(s). TUN=watch|status|both
	local/tunnel/tunnel.sh down $(TUN)
tunnel-status: ## Show tunnel(s) up/down. TUN=watch|status|both
	local/tunnel/tunnel.sh status $(TUN)
