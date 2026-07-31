# Contributor and release workflow

## Non-design boundary

This repository currently protects the home page and these article routes:

- `/code-power-and-consequences/`
- `/ethical-frameworks-in-development-of-technology/`
- `/freedom-of-speech-safe-online/`
- `/technology-companies-know-too-much-about-us/`

Do not change essay arguments, citation destinations, typography, colors, layout, header, footer, listing behavior, PaperMod theme behavior, or light/dark behavior as part of ingestion maintenance. A regenerated baseline is not approval. Follow `docs/preservation-baselines.md` and commit exact review evidence before an allowed reconciliation.

## Setup and validation

Use Hugo Extended `0.162.0` and the pinned PaperMod commit. Setup is offline and verifies exact theme bytes:

```bash
make setup
make validate
make reproducible
make build
make verify-routes
```

`make validate` runs deterministic unit/mutation tests, repository content validation, and preservation validation. Pull requests run the same validation without deployment permissions. Deployment uses only the validated artifact after an approved main/manual event. See `docs/reproducible-builds.md` for recovery.

## Ingestion

See `scripts/README.md` for the complete PDF/TXT contract. Never bypass a categorized failure. Corrupt, encrypted, sparse, partial, oversized, wrongly encoded, or citation-incomplete inputs require source repair or exact evidence; they are not candidates for permissive conversion. Publication uses no-replace atomic installation and every pre-commit failure must leave no post or staging debris.

## Citation evidence

Only explicit PDF annotations, visible HTTP(S) URLs, and explicit DOI forms are candidates. Matching is exact and deterministic. Do not search for or infer a plausible URL from author/title prose. Missing, ambiguous, duplicate, malformed, suspicious, orphaned, or unlinked evidence blocks publication.

Reviewed overrides live in `citation-overrides.json`, conform to `scripts/citation-overrides.schema.json`, and must identify one document, one citation, one already recovered destination, a reviewer, rationale, and exact evidence. Overrides are not waivers.

Committed article audits are under `audits/citations/`. `scripts/validate_content.py` rejects stale audits and unresolved parenthetical citation evidence. The dormant `cite`/`citations` shortcodes are unsupported compatibility files and cannot be used by posts; use reviewed inline links.

## Troubleshooting

- Tool/version error: install the pinned Hugo Extended/Poppler tools; do not float versions.
- Theme error: restore local theme changes, then run `make setup`.
- Citation error: inspect both JSON and text audit output, repair source evidence, or add a narrowly reviewed override.
- Existing destination: manually review the existing post; the converter never overwrites it.
- Route/build error: clean with `make build`, then run `make verify-routes`.
- Deployment error: rerun validation on a pull request; never bypass the Pages validation job.
