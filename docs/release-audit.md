# Release audit

## Automated status

The repository provides deterministic checks for pinned dependencies, reproducible builds, route/content/presentation preservation, hostile PDF/TXT ingestion, citation candidate recovery and matching, reviewed overrides, audit reports, front matter, links, and shortcode retirement.

Run:

```bash
make setup
make validate
make reproducible
make build
make verify-routes
```

## Existing article reconciliation

All four established files have committed machine-readable reports under `audits/citations/`. Existing routes, prose, and link destinations remain unchanged.

Publication remains intentionally blocked for two citation labels in `code-power-and-consequences.md` because this repository contains no explicit destination evidence for them:

- `(Lobel, 2017)`
- `(Lewis, 2014)`

Do **not** attach a plausible web result. Completion requires source PDF annotation/URL evidence or an explicit reviewer-supplied destination and evidence record. This is the required fail-closed outcome, not a validator defect.

## Shortcodes

The legacy `cite` and `citations` templates are retained only to avoid an unreviewed presentation-file change. They are not part of the normal render path, no current post invokes them, the archetype forbids them, and content validation blocks future use. Inline reviewed links are the supported citation representation.

## Manual release checklist

1. Resolve every blocking audit finding from explicit evidence.
2. Confirm `git diff --check` and the complete test suite pass.
3. Run clean reproducibility and route checks.
4. Review preservation diagnostics; never merely regenerate snapshots.
5. Review the GitHub Actions permissions and validated-artifact dependency.
6. Merge the hardened branch manually only after all checks are green.
