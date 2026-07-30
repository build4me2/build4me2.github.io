# Reproducible clean builds

Run build commands from the repository root after a recursive checkout:

```sh
git clone --recurse-submodules <repository-url>
cd <repository>
hugo version # must be Hugo Extended 0.162.0
make validate
make reproducible
make build
make verify-routes
```

`make setup` is the deterministic, network-free alternative when the PaperMod
submodule was not populated. It reconstructs and verifies the exact committed
theme snapshot. The Hugo installation and PaperMod commit requirements are
explained in [preservation-baselines.md](preservation-baselines.md).

## Entry points

- `make build` (or `python3 scripts/build_site.py`) creates `public/` from
  scratch. The command deletes the complete destination first and also passes
  Hugo's `--cleanDestinationDir`, so output removed or renamed in the sources
  cannot survive from an earlier build.
- `make reproducible` (or
  `python3 scripts/build_site.py --verify-reproducible`) performs two isolated
  clean builds with separate empty caches and compares a path-sorted SHA-256
  manifest. It fails with sorted per-path diagnostics if a path or any generated
  byte differs.
- `make verify-routes` checks the built deployment tree (without starting a
  server or accessing the network) for a non-empty home route, all four
  established article routes and titles, and every established home-list link.
  It reads the same preservation baseline used by the complete suite.
- `make validate` runs the complete offline test and preservation suite through
  `scripts/run_validation.py`. The runner globally sorts test IDs and normalizes
  checkout paths, randomized temporary fixture/build roots, object addresses,
  line endings, and unittest elapsed time so repeated successful or failing runs
  produce the same actionable report. Paths below a temporary root are retained
  (for example, `<temp>/fixtures/article.txt`) to keep diagnostics useful.

GitHub Pages uses the same build script after validation rather than maintaining
a second set of Hugo flags.

## Pull-request CI and deployment gate

The `.github/workflows/hugo.yml` clean recursive-checkout acceptance job initializes
recursive submodules, installs pinned Hugo Extended, and runs `make validate`,
`make reproducible`, `make build`, and `make verify-routes` for every pull request,
main-branch push, and manual dispatch. Thus the route check applies to the exact
clean deployment output, after the real isolated two-build byte comparison.
The shared validation/build job has only read access to repository contents; pull
requests receive no Pages write or OIDC token permission and cannot execute any
packaging or deployment job.

For a push to `main` or a manual dispatch, the successful shared job uploads its
already-validated `public/` output. A dependent Pages-packaging job downloads
that exact output (it does not rebuild), and the deployment job can run only
after both dependencies succeed. Pages write and OIDC permissions are scoped to
the approved-event jobs that need them. Recursive submodule checkout, serialized
Pages concurrency, the `github-pages` environment, and the existing Pages action
pipeline remain in effect.

## Deterministic environment contract

The build entry point verifies Hugo Extended **0.162.0**, disables cache reuse,
uses a fresh temporary cache, and fixes these inputs for every invocation:

| Input | Value |
| --- | --- |
| Hugo environment | `production` |
| Hugo clock | `2026-01-01T00:00:00Z` |
| `SOURCE_DATE_EPOCH` | `1767225600` |
| `TZ` | `UTC` |
| `LANG`, `LC_ALL` | `C.UTF-8` |

The fixed Hugo clock makes templates such as the existing copyright year stable;
it does not alter essay dates or prose. The build passes `--buildFuture` because
that fixed clock predates established publication dates; this keeps all four
established routes in deterministic output just as they appear in the live site.
Generated files are compared byte for
byte and are **not** rewritten or normalized. The only metadata excluded from
the reproducibility manifest is host filesystem metadata—mtime, ownership, and
permission bits—because Hugo assigns invocation-time mtimes and GitHub Pages
artifacts do not use those values as site content. File paths and file bytes are
always included. This is the complete normalization policy.

A successful check prints one stable summary line. Build failures remove
machine-specific destination/cache prefixes and elapsed durations from Hugo's
diagnostic while retaining the actionable error text.
