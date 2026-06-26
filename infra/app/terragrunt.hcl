# `app` stack — ECS Fargate service + ALB + task definition. (§4.3)
# Example stack wiring; the module itself lives in the Terragrunt lineage repo.

include "root" {
  path = find_in_parent_folders("terragrunt.hcl")
}

dependency "network" {
  config_path = "../network"
  mock_outputs = { vpc_id = "vpc-mock", private_subnet_ids = ["subnet-a", "subnet-b"] }
}

dependency "data" {
  config_path = "../data"
  mock_outputs = { db_secret_arn = "arn:aws:secretsmanager:::secret:mock", valkey_endpoint = "mock" }
}

# terraform { source = "git::git@github.com:gotoplanb/terragrunt-modules.git//ecs-service?ref=v1" }

inputs = {
  vpc_id             = dependency.network.outputs.vpc_id
  subnet_ids         = dependency.network.outputs.private_subnet_ids

  # Stateless tasks; sessions in Valkey (ADR-008). Secrets via the task def `secrets`
  # block (never inline environment) — referenced, not stored (§4.3).
  container_secrets = {
    DJANGO_SECRET_KEY    = "arn:aws:ssm:::parameter/watch/django-secret-key"
    POSTGRES_PASSWORD    = dependency.data.outputs.db_secret_arn
    INTAKE_WEBHOOK_SECRET = "arn:aws:ssm:::parameter/watch/intake-webhook-secret"
  }
  container_environment = {
    VALKEY_URL        = "redis://${dependency.data.outputs.valkey_endpoint}:6379/0"
    FLAGS_PROVIDER    = "appconfig"
    APPCONFIG_AGENT_URL = "http://localhost:2772"  # sidecar, identical path to local
  }

  # Blue/green via CodeDeploy: two target groups + test listener, alarm-gated shift,
  # BeforeAllowTraffic hook runs migrations + smoke (ADR-004 / §4.6).
  enable_blue_green = true
}
