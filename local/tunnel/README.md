# Dev tunnels (ngrok)

On-demand, basic-auth'd public ingress to the local dev servers on **stable reserved hostnames** —
for remote testing / sharing a running local stack before pushing to main. HTTP only; no SSH/TCP.

Two independent tunnels, toggled **separately or together**:

| tunnel  | local target             | domain (.env)          | agent dashboard |
|---------|--------------------------|------------------------|-----------------|
| `watch` | `:8010` (`make dev`)     | `TUNNEL_DOMAIN`        | :4045           |
| `status`| `:5173` (`make status-page`) | `STATUS_TUNNEL_DOMAIN` | :4046           |

Each agent's inspector web UI is pinned to a distinct port via a rendered per-agent config
(`agent.<svc>.local.yml`, `--config`) — clear of `:4040`, which the global `~/dev-infrastructure`
ngrok agent uses.

- **Durable (Terraform, `main.tf`):** the reserved ngrok domain(s) — stable hostnames. The status
  domain is optional (`count` on `STATUS_TUNNEL_DOMAIN`), so `watch` works standalone.
- **Runtime (`tunnel.sh`):** per-tunnel ngrok agent + edge **basic-auth** policy, each with its own
  pid / log / policy / web-addr, independent of the global `~/dev-infrastructure` LaunchAgent tunnels.

## One-time setup
1. In the repo `.env` (gitignored) set: `NGROK_API_KEY` (Terraform), `NGROK_AUTHTOKEN` (agent),
   `TUNNEL_DOMAIN` + (optional) `STATUS_TUNNEL_DOMAIN` (**your own** reserved hosts — the single
   source of truth for both the agent and Terraform), `TUNNEL_BASIC_AUTH_USER`,
   `TUNNEL_BASIC_AUTH_PASS`. See `.env.example`.
2. Reserve the domain(s): `make tunnel-domain` (both) — or scope with `TUN=watch` / `TUN=status`.

## Daily use
```
make tunnel-up                  # start BOTH — prints each https://<domain>
make tunnel-up   TUN=status     # just the status tunnel
make tunnel-down TUN=watch      # stop just the watch tunnel
make tunnel-status              # UP/DOWN per tunnel
```
`TUN` defaults to `both`; the underlying script also takes it positionally
(`local/tunnel/tunnel.sh up status`).

Notes:
- Rendered policies (`policy.<svc>.local.yml`, with creds), tfstate, and pid/log files are gitignored.
- If tofu state is lost: `tofu import ngrok_domain.watch_dev <domain>` (and `ngrok_domain.status_dev[0] <domain>`).
- **Status tunnel + the API (both tunnels up):** open the status page pointed at the watch tunnel:
  `https://<status-domain>/?api=https://<watch-domain>`. The SPA fetches `<hostname>:8010` by default,
  which is wrong over a tunnel (ngrok serves 443 only) — the `?api=` override (`index.html`) sends it to
  the watch tunnel instead. So the cross-origin fetch clears the watch edge, its **basic-auth policy
  exempts the public `/api/status*` posture endpoints** (`policy.tmpl.yml`; they're `AllowAny`, like the
  public status site) — everything else on the watch tunnel stays password-protected.
