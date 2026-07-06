# Reserved ngrok domain for the Watch local dev tunnel (`make dev` on :8010). This is the DURABLE
# piece — a stable public hostname so on-demand tunnels always resolve to the same URL. The tunnel
# AGENT and its edge basic-auth policy are runtime (see tunnel.sh), not Terraform. HTTP only; no
# SSH/TCP endpoints here by design.
#
# Auth: the provider reads NGROK_API_KEY from the environment (set it in the repo .env, gitignored).
# State is local (this dir, gitignored) — if lost, `tofu import ngrok_domain.watch_dev <domain>`.

provider "ngrok" {}

resource "ngrok_domain" "watch_dev" {
  domain      = var.domain
  description = "Watch local dev tunnel (make dev :8010) — on-demand, basic-auth at the edge."
}

# Status-page tunnel (make status-page :5173). Optional — created only when STATUS_TUNNEL_DOMAIN
# is set (count => 0/1), so the watch tunnel works standalone.
resource "ngrok_domain" "status_dev" {
  count       = trimspace(var.status_domain) == "" ? 0 : 1
  domain      = var.status_domain
  description = "Watch status-page local dev tunnel (make status-page :5173) — on-demand, basic-auth at the edge."
}

output "domain" {
  value = ngrok_domain.watch_dev.domain
}

output "url" {
  value = "https://${ngrok_domain.watch_dev.domain}"
}

output "status_domain" {
  value = try(ngrok_domain.status_dev[0].domain, "")
}

output "status_url" {
  value = try("https://${ngrok_domain.status_dev[0].domain}", "")
}
