#!/usr/bin/env bash
# On-demand ngrok tunnel to the local `make dev` server (:8010) on the reserved domain (Terraform,
# ./main.tf). Basic-auth at ngrok's edge (creds from the repo .env). HTTP only — no SSH/TCP.
# Self-contained: runs its OWN ngrok agent process (not the global LaunchAgent in
# ~/dev-infrastructure), so it is safe to toggle from here and won't disturb the other tunnels.
#
#   tunnel.sh domain  reserve the domain in ngrok (Terraform) from .env TUNNEL_DOMAIN
#   tunnel.sh up      start the tunnel in the background, print the URL
#   tunnel.sh down    stop it (and remove the rendered creds)
#   tunnel.sh status  is it up? show the public URL
#
# Requires in .env: TUNNEL_DOMAIN (the reserved host — the single source of truth for both the
# agent and Terraform), NGROK_AUTHTOKEN, TUNNEL_BASIC_AUTH_USER, TUNNEL_BASIC_AUTH_PASS. The
# `domain` step additionally needs NGROK_API_KEY (Terraform); the agent does not.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
PORT=8010
PIDFILE="$DIR/.tunnel.pid"
LOGFILE="$DIR/.tunnel.log"
POLICY_OUT="$DIR/policy.local.yml"

[ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }

resolve_domain() {
  if [ -n "${TUNNEL_DOMAIN:-}" ]; then printf '%s' "$TUNNEL_DOMAIN"; return; fi
  (cd "$DIR" && tofu output -raw domain 2>/dev/null)
}

case "${1:-}" in
  domain)
    command -v tofu >/dev/null || { echo "tofu not installed" >&2; exit 1; }
    : "${NGROK_API_KEY:?set NGROK_API_KEY in .env (Terraform reads it)}"
    : "${TUNNEL_DOMAIN:?set TUNNEL_DOMAIN in .env}"
    # TUNNEL_DOMAIN (.env) is the single source of truth -> feed it to the TF variable.
    export TF_VAR_domain="$TUNNEL_DOMAIN"
    ( cd "$DIR" && tofu init -input=false && tofu apply -auto-approve )
    ;;
  up)
    if running; then echo "already up: https://$(resolve_domain)  (pid $(cat "$PIDFILE"))"; exit 0; fi
    command -v ngrok >/dev/null || { echo "ngrok not installed (brew install ngrok/ngrok/ngrok)" >&2; exit 1; }
    : "${NGROK_AUTHTOKEN:?set NGROK_AUTHTOKEN in .env}"
    : "${TUNNEL_BASIC_AUTH_USER:?set TUNNEL_BASIC_AUTH_USER in .env}"
    : "${TUNNEL_BASIC_AUTH_PASS:?set TUNNEL_BASIC_AUTH_PASS in .env}"
    DOMAIN="$(resolve_domain)"; : "${DOMAIN:?no domain — run 'make tunnel-domain' or set TUNNEL_DOMAIN in .env}"
    # render the edge policy with creds (gitignored, owner-only)
    sed -e "s|__TUNNEL_USER__|${TUNNEL_BASIC_AUTH_USER}|g" \
        -e "s|__TUNNEL_PASS__|${TUNNEL_BASIC_AUTH_PASS}|g" \
        "$DIR/policy.tmpl.yml" > "$POLICY_OUT"
    chmod 600 "$POLICY_OUT"
    echo "Starting tunnel https://${DOMAIN} -> localhost:${PORT} (basic-auth) ..."
    # --url is the modern flag (ngrok agent >=3.5); older agents use --domain=${DOMAIN}
    NGROK_AUTHTOKEN="$NGROK_AUTHTOKEN" nohup ngrok http "$PORT" \
      --url="https://${DOMAIN}" \
      --traffic-policy-file="$POLICY_OUT" \
      >"$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    if running; then
      echo "up (pid $(cat "$PIDFILE")).  URL: https://${DOMAIN}   local dashboard: http://localhost:4040"
    else
      echo "failed to start — see $LOGFILE" >&2; rm -f "$PIDFILE" "$POLICY_OUT"; exit 1
    fi
    ;;
  down)
    if running; then kill "$(cat "$PIDFILE")" 2>/dev/null && echo "stopped"; else echo "not running"; fi
    rm -f "$PIDFILE" "$POLICY_OUT"
    ;;
  status)
    if running; then echo "UP    https://$(resolve_domain)  (pid $(cat "$PIDFILE"))"; else echo "DOWN"; fi
    ;;
  *)
    echo "usage: tunnel.sh {domain|up|down|status}" >&2; exit 2 ;;
esac
