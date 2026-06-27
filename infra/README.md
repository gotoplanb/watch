# infra — moved

Watch's cloud infrastructure (Terragrunt/OpenTofu for AWS, GitHub, Cloudflare) lives
in the **[`gotoplanb/platform`](https://github.com/gotoplanb/platform)** repo — the
estate-wide cloud IaC, cloud counterpart to `dev-infrastructure`.

- Deep rollout plan + sequence: `platform/ROLLOUT.md`
- Work tracking: the **AWS rollout epic** + sequenced issues in `gotoplanb/platform`
- Watch's app-side escalation code (`escalation/` ASL + Lambda handlers) stays here and
  is referenced by platform's `watch/.../escalation` stack.

Local development stays in this repo (`make dev`); only the **cloud** deployment moved.
