# Content and presentation preservation baseline

Run the offline preservation check from the repository root:

```sh
git submodule update --init --recursive
hugo version # must report v0.162.0 with Extended capabilities
python3 scripts/validate_preservation_baseline.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The rendering toolchain is pinned to **Hugo Extended 0.162.0**. Install that exact Extended release from the [Hugo releases](https://github.com/gohugoio/hugo/releases/tag/v0.162.0) before validating; do not substitute a newer patch or a standard build. GitHub Pages uses the same exact version through `HUGO_VERSION` in `.github/workflows/hugo.yml`.

The full `python3 scripts/validate_preservation_baseline.py` invocation is the required acceptance command; a `--source-only` run is diagnostic only and does not satisfy acceptance. Both modes first inspect the local `hugo env` output without accessing the network and report the expected and observed toolchains on a version or Extended-capability mismatch. Initialize the pinned submodule first. The validator checks both the committed PaperMod gitlink and the checked-out worktree commit before invoking Hugo; an absent or stale checkout reports the exact `git submodule update --init --recursive` recovery command instead of an indirect template error. The full check builds with an isolated cache in a temporary directory and leaves no `public/` tree. The mutation tests are also offline: they use temporary fixtures to prove that toolchain mismatches, empty HTTP route responses, missing routes, titles, listing entries, header/footer/theme-toggle markers, prose segments, configuration, templates, and styles produce focused failures.

## What is protected

`tests/baselines/preservation.json` is a reviewable inventory of:

- the four established source files, route slugs, titles, dates, and complete front matter;
- hashes of each exact essay body, each normalized prose segment, and its normalized rendered prose;
- every citation destination, including repeats and order, in both source and output;
- exact essay wording anchored to each article at `capturedFrom`, so changing both a post and its editable hash cannot hide an argument change;
- citation changes relative to `capturedFrom`, each of which must consume one exact, evidence-backed review record;
- the established articles' presence and relative order on the home page;
- canonical rendered element structure for the home and article pages;
- complete, sorted file inventories and hashes for local layouts and extended CSS, including header, footer, listing, citation, and theme-toggle overrides (new override files are rejected);
- the exact Hugo Extended version, behavior-relevant Hugo settings (including `defaultTheme = "auto"`), and the PaperMod gitlink commit.

The validator checks the baseline schema before reading any fixture values. Missing fields, incorrect JSON types, malformed hashes, and an incorrect established-entry count fail with sorted JSON-path diagnostics and no traceback. It then fails with a focused diagnostic when any protected value changes. It does not access the network and uses only Python's standard library, Git, and the locally installed Hugo executable.

## Change policy

Essay editing and reconciliation are deliberately different:

- **Prohibited:** changing an essay's argument or wording, established route identity, typography, layout, colors, post-list design, header/footer behavior, or current PaperMod light/dark behavior.
- **Potentially approvable reconciliation:** correcting front matter or a citation destination while preserving the argument and design. Approval is not implied merely by regenerating a hash.

Before changing a citation destination, add a `citation-reconciliation` entry to `tests/baselines/review-records.json` containing:

1. a unique non-empty `id`;
2. `article`, the repository-relative post path;
3. `citationIndex`, the destination's one-based position (repeats therefore remain unambiguous);
4. exact non-empty `before` and `after` destinations;
5. a non-empty `reason` and `verificationEvidence` array;
6. `proseArgumentRoutePresentationUnchanged: true`.

Then update only the affected destination and digests in `preservation.json`, run the full validator, and include the post, review record, and baseline in review. Each changed destination must consume exactly one matching record; stale, duplicate, or non-matching records fail validation. Prose is compared directly with the inherited `capturedFrom` source after only hyperlink destinations are masked, so rebaselining hashes cannot authorize changed wording or arguments. Never update baseline hashes to make an unexplained content or design change pass. The initial record identifies the inherited commit used for this inventory and approves no changes.
