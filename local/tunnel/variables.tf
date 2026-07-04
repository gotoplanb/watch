variable "domain" {
  description = "Reserved ngrok hostname for the dev tunnel. No default — each developer uses their OWN reserved domain (ngrok domains are account-scoped and globally unique). Sourced from TUNNEL_DOMAIN in .env via TF_VAR_domain by `make tunnel-domain`. A *.ngrok.app subdomain (paid), or a custom domain you own (then wire the CNAME from cname_target)."
  type        = string

  validation {
    condition     = length(trimspace(var.domain)) > 0
    error_message = "domain is required — set TUNNEL_DOMAIN=<your-reserved-host> in .env (make tunnel-domain maps it to TF_VAR_domain)."
  }
}

