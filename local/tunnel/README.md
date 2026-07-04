# Dev tunnel (ngrok)

On-demand, basic-auth'd public ingress to the local `make dev` server (`:8010`) on a **stable
reserved hostname** — for remote testing / sharing a running local server before pushing to main.
HTTP only; no SSH/TCP.

- **Durable (Terraform, `main.tf`):** the reserved ngrok domain — the stable hostname.
- **Runtime (`tunnel.sh`):** the ngrok agent + edge **basic-auth** policy, toggled on demand. Runs
  its own agent process, independent of the global `~/dev-infrastructure` LaunchAgent tunnels.

## One-time setup
1. In the repo `.env` (gitignored) set: `NGROK_API_KEY` (Terraform), `NGROK_AUTHTOKEN` (agent),
   `TUNNEL_DOMAIN` (or accept the `variables.tf` default), `TUNNEL_BASIC_AUTH_USER`,
   `TUNNEL_BASIC_AUTH_PASS`. See `.env.example`.
2. Reserve the domain: `make tunnel-domain` (runs `tofu apply` here).

## Daily use
```
make tunnel-up       # start — prints https://<domain>
make tunnel-status   # UP/DOWN
make tunnel-down     # stop (also wipes the rendered creds file)
```

Notes: the rendered policy (`policy.local.yml`, with creds), tfstate, pid/log are all gitignored.
If tofu state is lost: `tofu import ngrok_domain.watch_dev <domain>`. Custom (non-`ngrok.app`)
domains need a CNAME to the resource's `cname_target`.
