variable "domain" {
  description = "Reserved ngrok hostname for the dev tunnel. No default — each developer uses their OWN reserved domain (ngrok domains are account-scoped and globally unique). Sourced from TUNNEL_DOMAIN in .env via TF_VAR_domain by `make tunnel-domain`. A *.ngrok.app subdomain (paid), or a custom domain you own (then wire the CNAME from cname_target)."
  type        = string

  validation {
    condition     = length(trimspace(var.domain)) > 0
    error_message = "domain is required — set TUNNEL_DOMAIN=<your-reserved-host> in .env (make tunnel-domain maps it to TF_VAR_domain)."
  }
}

variable "status_domain" {
  description = "Reserved ngrok hostname for the status-page tunnel (:5173). Sourced from STATUS_TUNNEL_DOMAIN in .env via TF_VAR_status_domain. Optional — empty means the status tunnel isn't reserved (the watch tunnel is unaffected)."
  type        = string
  default     = ""
}

