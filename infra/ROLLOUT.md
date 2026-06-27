# AWS rollout plan

Getting Watch running on AWS the way the ADRs specify: ECS Fargate behind an ALB,
**CodeDeploy blue/green**, AppConfig **feature flags**, Step Functions **escalation**,
durable **intake**, S3+CloudFront **status page**, OTel → **Watchtower**, all via
**Terragrunt** with **GitHub validates / AWS adjudicates** (ADR-004).

## Decisions for this rollout
- **Single AWS account, two envs:** `staging` + `prod` (staging exercises the full
  pipeline + blue/green + migrations before prod). Single-region, Multi-AZ (ADR-005).
- **GitHub → AWS via OIDC** role assumption — no long-lived keys.
- **This phase = author + validate IaC; apply deferred.** No AWS access in the build
  sandbox, so we write all Terragrunt + Lambda packaging and run `tofu validate` / `fmt`
  / hcl checks; `terraform apply` is gated on real credentials.
- Everything else follows the ADRs (ECS Fargate, RDS Postgres Multi-AZ, ElastiCache
  Valkey, AppConfig, Step Functions, Terragrunt — ADR-001/003/005/006/010).

## Repo layout (Terragrunt)
```
infra/
  terragrunt.hcl                 # root: remote_state (S3+DynamoDB) + provider gen
  _envcommon/                    # shared stack inputs (DRY) per stack type
  <account>/<region>/
    staging/{network,data,secrets,app,escalation,intake,frontend,pipeline,observability}/
    prod/   {network,data,secrets,app,escalation,intake,frontend,pipeline,observability}/
  bootstrap/                     # state backend + GitHub OIDC (chicken-and-egg; see #0)
  modules/ (or git:: refs)       # reusable modules per stack
```

## Sequence & dependencies
| # | Phase | Depends on | Key resources |
|---|-------|-----------|---------------|
| 00 | Local AWS access (operator prereq) | — | AWS CLI v2, account + region, bootstrap credential / SSO profile, `aws sts get-caller-identity` |
| 0a | State backend + Terragrunt structure | 00 | S3 state bucket, DynamoDB lock, root `terragrunt.hcl`, `_envcommon` |
| 0b | GitHub OIDC + IAM roles | 0a | OIDC provider, per-env deploy roles (least-priv), CI assume-role |
| 1 | `network` | 0 | VPC, public/private subnets ×AZ, NAT, SGs, VPC endpoints (ECR/S3/SSM/logs) |
| 2 | `data` | 1 | RDS Postgres Multi-AZ + Secrets Manager rotation, ElastiCache Valkey, subnet/param groups, KMS |
| 3 | `secrets` + AppConfig | 1 | SSM SecureString params, Secrets Manager entries; AppConfig app/env/profile + flags (ADR-003) |
| 4 | `app` (ECS) | 2,3 | ECR repo, ECS Fargate cluster/service, ALB **2 target groups + test listener**, task def (AppConfig Agent sidecar, `secrets` block), autoscaling |
| 5 | `escalation` | 2,4 | Step Functions Standard (rendered ASL, real ARNs/SLAs), `record_token`/`commit` Lambdas, IAM, **CloudWatch alarm on failed executions** (ADR-001) |
| 6 | `intake` | 1,2 | API Gateway (webhook, shared-secret authz) → SQS → consumer; DLQ; ack-on-enqueue (ADR-002) |
| 7 | `frontend` | 0 | S3 + CloudFront (OAC), fingerprinted assets + short-TTL `index.html` (ADR-005), build→sync→invalidation |
| 8 | `pipeline` (CD) | 4,5,6,7 | CodePipeline → CodeBuild (authoritative test/coverage/Sonar/build/push) → **CodeDeploy ECS blue/green** (alarm-gated canary/linear, auto-rollback, `BeforeAllowTraffic`/`AfterAllowTraffic` hooks), GitHub status-back; GitHub Actions = lint/format only (ADR-004) |
| 9 | `observability` | 4 | ECS OTel → Watchtower (traces+metrics+logs, proven locally), CloudWatch alarms, **masked drains** (Logs data-protection), SmokeShow E2E hook (#7) |
| 10 | Expand→contract migration | 4,8 | One real migration through expand → migrate → backfill → cut-over → **contract (separate release)** + runbook (§4.9) |
| 11 | DNS / TLS | 4,7 | Cloudflare DNS + ACM certs (ALB + CloudFront), domain wiring (ADR-006) |

## Blue/green mechanics (ADR-004 / §4.6)
Two ALB target groups + a **test listener**; CodeDeploy shifts traffic
canary/linear **gated on CloudWatch alarms**; `BeforeAllowTraffic` runs **migrations +
smoke** against the green task set, `AfterAllowTraffic` validates; **automatic rollback**
on alarm. The authoritative gates (the same `make coverage` + `make sonar-scan` we run
locally) run in **CodeBuild**, so they can't be bypassed with `--no-verify` (closes the
local-hook gap, ADR-004).

## Cross-cutting
- **Secrets** are referenced, never inlined: SSM SecureString (static) / Secrets Manager
  (RDS rotation) via the task-def `secrets` block (§4.3). No secrets in images or `environment`.
- **Flags** keep the `flags.is_enabled` seam; AppConfig Agent sidecar gives the identical
  `localhost:2772` path in ECS as locally (ADR-003).
- **Escalation Lambdas** package the existing `escalation/lambdas/{record_token,commit}.py`
  handlers (which call `incidents.services`) — same code proven locally via the shim (ADR-010).

## Open inputs (needed before `apply`)
AWS account id, region, domain name (Cloudflare zone), and whether **Watchtower is
reachable from AWS** (or we run an OTel Collector/Alloy in-VPC that forwards to it). Paging
(ntfy, ADR-013 / issue #8) layers on after the app tier is live.

## Apply order
00 (operator: CLI + bootstrap creds) → 0a → 0b → 1 → (2,3 parallel) → (6 intake parallel
with 4 app) → 4 → 5 → 7 → 8 → 9 → 10/11.
Rollback is per-stack `destroy` in reverse, but prefer forward-fix; blue/green handles app
rollback automatically.
