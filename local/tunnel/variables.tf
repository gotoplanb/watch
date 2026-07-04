variable "domain" {
  description = "Reserved ngrok hostname for the dev tunnel — a *.ngrok.app subdomain (paid), or a custom domain you own (then wire the CNAME from cname_target)."
  type        = string
  default     = "watch-dev.ngrok.app"
}
