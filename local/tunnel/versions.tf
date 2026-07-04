terraform {
  required_version = ">= 1.6"
  required_providers {
    ngrok = {
      source  = "ngrok/ngrok"
      version = "~> 0.8"
    }
  }
}
