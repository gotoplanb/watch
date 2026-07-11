# Releasing Watch

How we cut releases and what "ready" means. **Releases are git tags** — the app version isn't kept
in a file (`backend/pyproject.toml` is stale by design). Three repos move together:

- **watch** — the application (this repo); carries the real per-release notes.
- **platform** — the cloud estate + delivery pipeline (`~/platform`).
- **watchtower** — the observability + SonarQube stack (`~/watchtower`).

For **minor** and **major** releases the three are tagged **in lockstep with the same version** as a
**tested-together, verified-compatible attestation** — the tag on platform/watchtower says "this
estate/observability was verified to run this app," even when those repos have no code change.
**Patch** versions **may diverge** across the three.

Per-release notes live here as `docs/releases/vX.Y.md` — **one file per minor line**, with a
**summary at the very top** followed by the detailed description.

**The tiers build on each other: `patch ⊂ minor ⊂ major`.** Each tier does **everything the lighter
tier does, plus** the extra steps its own section lists. A **minor** is a patch **plus** the notes +
lockstep ceremony; a **major** is a minor **plus** the audit/communication ceremony. So each section
states only what its tier *adds* — nothing needs to spell out what it excludes.

---

## Patch — `vX.Y.Z`  ·  *the base every release includes*

Ship the fix; tag it only if you want a marker. That's the whole of a patch — it's watch-only, so it
doesn't drag platform/watchtower along.

## Minor — `vX.Y.0`  ·  *current cadence*

**Everything a patch does, plus:**

1. **Write the detailed release note** — `docs/releases/vX.Y.md`: a **summary at the very top**, then
   a thorough account of everything in the release (themes, ADRs delivered, DB migrations, operator
   notes, an at-a-glance table). Commit it.
2. **Verify the three integrate**, then **tag all three repos** with the same annotated `vX.Y.0`.
   watch carries the real changes; **platform + watchtower get the same tag as a compatibility
   attestation** (a brief release body, even with no changes).
3. **Create the GitHub release on each repo and post the summary as the release body** — watch gets
   the full summary; platform/watchtower get a short attestation that links back to watch's notes doc.

Worked example (v0.8):

```bash
# watch — full notes + release
git tag -a v0.8.0 -F notes.md && git push origin v0.8.0
gh release create v0.8.0 --repo gotoplanb/watch \
  --title "v0.8.0 — …" --notes-file notes.md

# platform + watchtower — same tag, brief attestation body linking to watch's docs/releases/v0.8.md
git -C ~/platform   tag -a v0.8.0 -F attest.md && git -C ~/platform   push origin v0.8.0
git -C ~/watchtower tag -a v0.8.0 -F attest.md && git -C ~/watchtower push origin v0.8.0
gh release create v0.8.0 --repo gotoplanb/platform   --title "…" --notes-file attest.md
gh release create v0.8.0 --repo gotoplanb/watchtower --title "…" --notes-file attest.md
```

## Major — `vX.0.0`  ·  *forming — not yet exercised*

**Everything a patch and minor do, plus** the extra ceremony a major warrants. Known requirements so
far (fill in the rest when we cut the first major):

1. **Documentation & ADR audit** — a thorough review of **all ADRs** (`watch-adrs.md`) and the
   architecture/design docs (spec, `docs/`, platform `docs/architecture/*`) to confirm they reflect
   the shipped reality. Reconcile any drift **in the same release** — per the "code follows the ADRs"
   discipline, a major is the checkpoint where the written record must catch up to the code.
2. **Demo clips** — record clips of the user-facing features (`make demo` storyboards) and attach them
   to the GitHub release. A major is the milestone worth a visual walkthrough of the accumulated
   user-facing surface (a per-minor UI clip would just be churn).
3. **Breaking-change / upgrade / migration communication** — call out anything that isn't a
   drop-in upgrade (schema, config, API, or cross-repo contract changes).
4. **Pin dependencies aggressively.** During rapid build we float versions for velocity; a major is
   when we lock the toolchain and libraries — OpenTofu / Terragrunt / provider versions, language
   runtimes, key app deps — so the release is reproducible and an unpinned upgrade can't silently
   break us. (Real example: OpenTofu auto-upgrading to 1.12 introduced a resource-identity check that
   broke cross-account `teardown` mid-build. `-refresh=false` looked like a workaround but made it
   *worse* — it dropped state entries without deleting the resources, leaving billable orphans — and
   was reverted; the real fix was **pinning OpenTofu to 1.11.11** and asserting the pin in the scripts
   and CI. See ADR-032 and `docs/releases/v0.9.md`.) Record the pinned versions in the release notes.
5. **AWS IAM policy audit — least privilege + completeness.** Review the estate's roles/policies
   (platform `modules/**`) so every principal has **exactly** what it needs: **no more** (drop
   `Resource = "*"` and unused actions in favour of scoped ARNs + conditions; document any intentional
   wildcard) and **no less** (no missing grants that only fail the first time a code path actually
   runs). A major is when to tighten the broad grants taken for build-velocity **and** to shake out
   latent gaps. (Real example: the CodeBuild build role was missing `kms:Decrypt` on the artifact CMK
   and tried to `update-function-code` cross-account prod Lambdas in-account — both invisible for
   months because the new-account CodeBuild hold meant **Build never ran**; the fallback-account
   cutover unblocked CodeBuild and surfaced them. See platform `docs/architecture/accounts.md`.)
   Cross-account trust policies (`watch-prod-deploy`, OIDC providers) get the same review.

> Still forming — expand this as we learn what a major actually needs.

---

_See also: `watch-adrs.md` for the decisions behind each release, and the per-release `vX.Y.md` notes
in this directory._
