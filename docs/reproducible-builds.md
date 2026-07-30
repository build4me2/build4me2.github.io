# Reproducible clean builds

Run build commands from the repository root after a recursive checkout:

```sh
git clone --recurse-submodules <repository-url>
cd <repository>
hugo version # must be Hugo Extended 0.162.0
make validate
make build
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
- `make validate` runs the complete offline test and preservation suite.

GitHub Pages uses the same build script after validation rather than maintaining
a second set of Hugo flags.

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
it does not alter essay dates or prose. Generated files are compared byte for
byte and are **not** rewritten or normalized. The only metadata excluded from
the reproducibility manifest is host filesystem metadata—mtime, ownership, and
permission bits—because Hugo assigns invocation-time mtimes and GitHub Pages
artifacts do not use those values as site content. File paths and file bytes are
always included. This is the complete normalization policy.

A successful check prints one stable summary line. Build failures remove
machine-specific destination/cache prefixes and elapsed durations from Hugo's
diagnostic while retaining the actionable error text.
