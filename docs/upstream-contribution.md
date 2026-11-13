# Reviewing an upstream contribution

A contribution is approved for publication only after its source, history and exact
artifacts have been reviewed. Tests and secret scanners support that review; they do
not establish that customer information is absent and do not authorize a push.

## Check repository fit before building

Read the ADRs on current upstream main, including accepted designs marked not yet
built. Implement the existing decision or amend it in the same PR; make any departure
explicit rather than introducing a parallel design labeled as a refinement.

Reuse the established intake/outbound-event, queue-worker and feature-flag seams.
External-reaching behavior defaults off. Persist raw actor identities and resolve
labels for display. Follow the Tailwind-utilities convention until a change in UI
direction is explicitly accepted by the maintainer.

A coherent feature can remain one PR. Use a few logical commits that make the model,
lifecycle, worker, UI and documentation changes reviewable. Include a **Deviations /
decisions needed** section for ADR/convention departures and changes in product
navigation or default landing behavior. Those decisions need explicit maintainer
confirmation rather than being implied by the implementation.

Include a **Local-only vs cloud** statement for any new runtime process or platform
requirement. Supply the infrastructure integration or identify the remaining cloud
follow-up clearly. Before opening, rebase on current main and report unit/coverage,
integration, Playwright and Sonar results separately; name every check not run and
why. Publication-content checks below establish a different boundary and do not
replace these codebase-fit and quality checks.

## Scope the candidate

1. Refresh the upstream base using read-only Git fetch. Record its exact commit and
   confirm which local commits would be introduced. Review author metadata, commit
   messages, historical file versions, stashes and the proposed tree. Existing public
   upstream findings must be distinguished from newly introduced information.
2. Select changed/new source paths explicitly. Work from a clean export or separate
   checkout, with no application database, environment file, cache, telemetry,
   credentials, screenshots, mail, browser traces, document packages or Git metadata.
   Include new files deliberately rather than copying the working directory.
3. Review fixtures, tests, documentation, comments, URLs, query strings, identifiers,
   timestamps, numbers, topology and encoded assets. Replacing names alone is
   insufficient: incident sequences and measurement combinations can identify an
   event. Invent demonstration scenarios independently, using fictional entities and
   reserved example domains. Do not translate a private RCA into a public fixture.
4. Capture product decisions as generic Context/Decision/Consequences ADRs. Explain
   the workflow and tradeoffs without the source document's customer names, sample
   measurements, actual incidents, screenshots, transcript text or document metadata.
   Do not present illustrative thresholds as customer contracts or production policy.

## Run local content and secret checks

Build a private vocabulary from authorized source material: customer/supplier names,
aliases, internal system names, hosts, email addresses, ticket IDs, account/resource
identities and meaningful variants. Keep that vocabulary and source comparisons
outside the publication tree and out of reports that will be shared upstream.
Review long phrase overlaps and combinations of facts as well as exact names.
Never submit private terms or source material to a hosted scanner or search engine.

The repository's content guard runs locally and writes only file hashes, locations
and numeric term IDs; it does not echo private vocabulary:

```sh
python3 scripts/check_publication.py /absolute/path/source-only-tree \
  --private-terms /absolute/path/private-terms.json \
  --manifest /absolute/path/review-manifest.json
make publication-test
```

The vocabulary file is a JSON array of strings. The source-only export must not
contain it or the review manifest. The guard rejects common runtime/binary artifacts,
symlinks, private machine paths and cloud account locators. The all-zero local
emulator account and documented service-container homes are recognized as generic
fixtures; nonzero account locators and workstation identities still require review. It is intentionally
conservative; resolve each finding against the actual source rather than adding a
broad allowlist. It cannot recognize every customer-specific fact.

Run a locally installed, verified secret scanner against both the source-only tree
and reachable Git history. For example, Gitleaks can produce fully redacted JSON:

```sh
gitleaks dir /absolute/path/source-only-tree --redact=100 --ignore-gitleaks-allow \
  --report-format=json --report-path=/absolute/path/tree-secrets.json
gitleaks git . --log-opts=--all --redact=100 --ignore-gitleaks-allow \
  --report-format=json --report-path=/absolute/path/history-secrets.json
```

Inspect every result. A hard-coded test value may be a false positive, but validate
that fact in context and record its exact location and provenance. Do not ignore an
entire tests directory, change scanner exit behavior to hide findings, or treat a
clean current tree as proof that historical credentials were never exposed. A real
exposed secret requires revocation/rotation; changing a file does not revoke it.

## Freeze and approve

Record the base commit, explicit paths, per-file hashes/modes, archive hash, scanner
version/checksum/configuration, findings and dispositions, source coverage and gaps,
manual review notes and functional test results in a private review report. Keep
unresolved findings visible. If a public baseline contains source-specific labels,
removing them from the candidate does not erase upstream's existing history; that
requires a separate maintainer decision. A patch can repeat deleted/context lines,
so review the patch itself as an artifact too. A source-only overlay avoids bundling
old deleted text; the eventual Git diff still needs review against the upstream base.

Before using the approved snapshot, verify its seal without regenerating it:

```sh
python3 scripts/check_publication.py /absolute/path/source-only-tree \
  --manifest /absolute/path/review-manifest.json --verify
```

Have an authorized person who understands the source review the exact candidate and
report. Approval names a specific base and artifact hash; any content, mode, history
or artifact change invalidates that approval. Do not attach private review evidence,
original documents or runtime examples to a PR.

After explicit publication authorization, apply only the approved files to a clean
branch from the reviewed upstream base, verify the resulting tree and diff, run the
required quality checks, and confirm the intended fork remote and commit identity.
Push only that reviewed branch to the authorized fork and open the upstream PR using
a generic description. Do not push accumulated working branches or copied Git
history. Record the resulting commit/PR and retain the private approval evidence.
