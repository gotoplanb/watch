# Infrastructure (Terragrunt)

Terraform/Terragrunt over CDK (ADR-006): the estate spans providers (AWS,
Cloudflare, GitHub org) and a readable `plan` diff is itself an audit artifact.

## Stacks (§4.7)
Separate stacks, applied in dependency order:

| Stack | Owns |
|---|---|
| `network` | VPC, subnets across AZs, security groups |
| `data` | RDS Postgres (Multi-AZ), ElastiCache Valkey |
| `app` | ECS Fargate service, ALB, task def (AppConfig Agent sidecar, `secrets` block) |
| `escalation` | Step Functions state machine + record_token/auto_escalate Lambdas, CloudWatch alarm on failed executions (ADR-001) |
| `pipeline` | CodePipeline → CodeBuild (authoritative gates) → CodeDeploy ECS blue/green (ADR-004) |
| `frontend` | S3 + CloudFront, fingerprinted assets / short-TTL index.html (ADR-005) |

## Conventions
- `terragrunt.hcl` at the root holds remote state + provider generation; each stack
  inherits via `include`.
- Secrets are **referenced**, never stored: SSM SecureString (static) / Secrets
  Manager (RDS rotation), surfaced through the task def `secrets` block (§4.3).
- One real expand→migrate→backfill→cut-over→contract migration is the worked
  example (§4.9); contract is a separate later release gated on SLOs unchanged.

> Scaffold: `terragrunt.hcl` (root) and `app/terragrunt.hcl` show the pattern; the
> remaining stacks are to be filled from the existing multi-stack Terragrunt lineage.
