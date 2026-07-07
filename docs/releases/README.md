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

---

## Patch — `vX.Y.Z`

**No ceremony required.** Ship the fix; tag only if you want a marker. No detailed notes doc, no
lockstep — a watch-only patch doesn't drag platform/watchtower along.

## Minor — `vX.Y.0`  ·  *current cadence*

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

## Major — `vX.0.0`  ·  *TODO — not yet exercised*

> **Placeholder.** We haven't cut a major release yet. When we do, this section should define the
> extra ceremony on top of the minor checklist — at minimum: recording **demo clips** of the
> user-facing features (`make demo` storyboards) and attaching them to the GitHub release, plus
> whatever breaking-change / upgrade / migration communication a major warrants. Fill this in when we
> get there.

---

_See also: `watch-adrs.md` for the decisions behind each release, and the per-release `vX.Y.md` notes
in this directory._
