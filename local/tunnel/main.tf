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

output "domain" {
  value = ngrok_domain.watch_dev.domain
}

output "url" {
  value = "https://${ngrok_domain.watch_dev.domain}"
}
