#!/usr/bin/env bash
# On-demand ngrok tunnels to the local dev servers, on stable reserved domains (Terraform, ./main.tf).
# TWO services, startable/stoppable separately or together:
#   watch  -> localhost:8010  (the `make dev` backend),     domain = .env TUNNEL_DOMAIN
#   status -> localhost:5173  (the `make status-page` SPA),  domain = .env STATUS_TUNNEL_DOMAIN
# Basic-auth at ngrok's edge (creds from .env). HTTP only. Each service runs its OWN agent process
# with its own pid / log / policy / web-addr, so toggling one never disturbs the other.
#
#   tunnel.sh domain [watch|status|both]   reserve the domain(s) in ngrok (Terraform) from .env
#   tunnel.sh up     [watch|status|both]   start tunnel(s), print the URL(s)
#   tunnel.sh down   [watch|status|both]   stop tunnel(s) (and remove rendered creds)
#   tunnel.sh status [watch|status|both]   up/down per service
# The target defaults to `both`.
#
# .env: TUNNEL_DOMAIN (+ STATUS_TUNNEL_DOMAIN for the status tunnel) — reserved hosts, the single
# source of truth for both the agent and Terraform; NGROK_AUTHTOKEN; TUNNEL_BASIC_AUTH_USER/PASS.
# The `domain` step additionally needs NGROK_API_KEY (Terraform); the agent does not.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
[ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }

# --- per-service config ---------------------------------------------------------------------------
svc_port()      { case "$1" in watch) echo 8010 ;; status) echo 5173 ;; *) return 1 ;; esac; }
svc_web()       { case "$1" in watch) echo "localhost:4045" ;; status) echo "localhost:4046" ;; esac; }  # clear of :4040 (global agent)
svc_tf()        { case "$1" in watch) echo "ngrok_domain.watch_dev" ;; status) echo "ngrok_domain.status_dev" ;; esac; }
svc_domain()    { case "$1" in watch) printf '%s' "${TUNNEL_DOMAIN:-}" ;; status) printf '%s' "${STATUS_TUNNEL_DOMAIN:-}" ;; esac; }
svc_domainvar() { case "$1" in watch) echo "TUNNEL_DOMAIN" ;; status) echo "STATUS_TUNNEL_DOMAIN" ;; esac; }
pidfile() { echo "$DIR/.tunnel.$1.pid"; }
logfile() { echo "$DIR/.tunnel.$1.log"; }
polfile() { echo "$DIR/policy.$1.local.yml"; }
cfgfile() { echo "$DIR/agent.$1.local.yml"; }

running() { local pf; pf="$(pidfile "$1")"; [ -f "$pf" ] && kill -0 "$(cat "$pf" 2>/dev/null)" 2>/dev/null; }

expand_targets() {
  case "${1:-both}" in
    watch)       echo watch ;;
    status)      echo status ;;
    both|all|"") echo watch status ;;
    *) echo "unknown target '$1' (want: watch | status | both)" >&2; return 2 ;;
  esac
}

render_policy() { # $1=svc — render the shared basic-auth template with creds (gitignored, mode 600)
  sed -e "s|__TUNNEL_USER__|${TUNNEL_BASIC_AUTH_USER}|g" \
      -e "s|__TUNNEL_PASS__|${TUNNEL_BASIC_AUTH_PASS}|g" \
      "$DIR/policy.tmpl.yml" > "$(polfile "$1")"
  chmod 600 "$(polfile "$1")"
}

render_config() { # $1=svc — per-agent config with a distinct web_addr so two agents don't clash on :4040
  printf 'version: "3"\nagent:\n  web_addr: %s\n' "$(svc_web "$1")" > "$(cfgfile "$1")"
}

up_one() {
  local svc="$1" port domain
  port="$(svc_port "$svc")" || { echo "unknown service '$svc'" >&2; return 2; }
  domain="$(svc_domain "$svc")"
  if running "$svc"; then echo "$svc: already up  https://$domain  (pid $(cat "$(pidfile "$svc")"))"; return 0; fi
  command -v ngrok >/dev/null || { echo "ngrok not installed (brew install ngrok/ngrok/ngrok)" >&2; return 1; }
  : "${NGROK_AUTHTOKEN:?set NGROK_AUTHTOKEN in .env}"
  : "${TUNNEL_BASIC_AUTH_USER:?set TUNNEL_BASIC_AUTH_USER in .env}"
  : "${TUNNEL_BASIC_AUTH_PASS:?set TUNNEL_BASIC_AUTH_PASS in .env}"
  if [ -z "$domain" ]; then
    echo "$svc: no domain — set $(svc_domainvar "$svc") in .env (or 'make tunnel-domain TUN=$svc')" >&2; return 1
  fi
  render_policy "$svc"; render_config "$svc"
  echo "$svc: starting https://${domain} -> localhost:${port} (basic-auth) ..."
  NGROK_AUTHTOKEN="$NGROK_AUTHTOKEN" nohup ngrok http "$port" \
    --url="https://${domain}" \
    --config="$(cfgfile "$svc")" \
    --traffic-policy-file="$(polfile "$svc")" \
    >"$(logfile "$svc")" 2>&1 &
  echo $! > "$(pidfile "$svc")"
  sleep 3
  if running "$svc"; then
    echo "$svc: up (pid $(cat "$(pidfile "$svc")"))  URL: https://${domain}   agent dashboard: http://$(svc_web "$svc")"
    # The status SPA needs its API pointed at the watch tunnel — print the ready-to-open URL.
    local watch_domain; watch_domain="$(svc_domain watch)"
    [ "$svc" = "status" ] && [ -n "$watch_domain" ] && \
      echo "        open: https://${domain}/?api=https://${watch_domain}   (status page -> watch tunnel API)"
  else
    echo "$svc: failed to start — see $(logfile "$svc")" >&2; rm -f "$(pidfile "$svc")" "$(polfile "$svc")" "$(cfgfile "$svc")"; return 1
  fi
}

down_one() {
  local svc="$1"
  if running "$svc"; then kill "$(cat "$(pidfile "$svc")")" 2>/dev/null && echo "$svc: stopped"; else echo "$svc: not running"; fi
  rm -f "$(pidfile "$svc")" "$(polfile "$svc")" "$(cfgfile "$svc")"
}

status_one() {
  local svc="$1" domain; domain="$(svc_domain "$svc")"
  if running "$svc"; then echo "$svc   UP    https://${domain}  (pid $(cat "$(pidfile "$svc")"))"
  else echo "$svc   DOWN  ${domain:+https://$domain}"; fi
}

domain_cmd() { # reserve via Terraform; -target the named service's resource, or the whole config for `both`
  command -v tofu >/dev/null || { echo "tofu not installed" >&2; exit 1; }
  : "${NGROK_API_KEY:?set NGROK_API_KEY in .env (Terraform reads it)}"
  : "${TUNNEL_DOMAIN:?set TUNNEL_DOMAIN in .env}"
  export TF_VAR_domain="$TUNNEL_DOMAIN"
  export TF_VAR_status_domain="${STATUS_TUNNEL_DOMAIN:-}"
  local svcs; svcs="$(expand_targets "${1:-both}")" || exit 2
  local targets=()
  if [ "$(echo "$svcs" | wc -w | tr -d ' ')" = "1" ]; then
    [ "$svcs" = status ] && [ -z "${STATUS_TUNNEL_DOMAIN:-}" ] && { echo "status: set STATUS_TUNNEL_DOMAIN in .env first" >&2; exit 1; }
    targets=(-target="$(svc_tf "$svcs")")
  fi
  ( cd "$DIR" && tofu init -input=false && tofu apply -auto-approve "${targets[@]}" )
}

CMD="${1:-}"; TARGET="${2:-both}"
if [ "$CMD" = "domain" ]; then domain_cmd "$TARGET"; exit $?; fi

SVCS="$(expand_targets "$TARGET")" || { echo "usage: tunnel.sh {domain|up|down|status} [watch|status|both]" >&2; exit 2; }
case "$CMD" in
  up)     for s in $SVCS; do up_one "$s"; done ;;
  down)   for s in $SVCS; do down_one "$s"; done ;;
  status) for s in $SVCS; do status_one "$s"; done ;;
  *) echo "usage: tunnel.sh {domain|up|down|status} [watch|status|both]" >&2; exit 2 ;;
esac
