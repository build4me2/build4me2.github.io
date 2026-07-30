# Content and presentation preservation baseline

Run the offline preservation check from the repository root:

```sh
git submodule update --init --recursive
python3 scripts/validate_preservation_baseline.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The full `python3 scripts/validate_preservation_baseline.py` invocation is the required acceptance command; a `--source-only` run is diagnostic only and does not satisfy acceptance. Initialize the pinned submodule first. The full check builds with an isolated cache in a temporary directory and leaves no `public/` tree. The mutation tests are also offline: they use temporary fixtures to prove that empty or missing routes, titles, listing entries, header/footer/theme-toggle markers, prose segments, configuration, templates, and styles produce focused failures.

## What is protected

`tests/baselines/preservation.json` is a reviewable inventory of:

- the four established source files, route slugs, titles, dates, and complete front matter;
- hashes of each exact essay body, each normalized prose segment, and its normalized rendered prose;
- every citation destination, including repeats and order, in both source and output;
- the established articles' presence and relative order on the home page;
- canonical rendered element structure for the home and article pages;
- complete, sorted file inventories and hashes for local layouts and extended CSS, including header, footer, listing, citation, and theme-toggle overrides (new override files are rejected);
- behavior-relevant Hugo settings (including `defaultTheme = "auto"`) and the PaperMod gitlink commit.

The validator checks the baseline schema before reading any fixture values. Missing fields, incorrect JSON types, malformed hashes, and an incorrect established-entry count fail with sorted JSON-path diagnostics and no traceback. It then fails with a focused diagnostic when any protected value changes. It does not access the network and uses only Python's standard library, Git, and the locally installed Hugo executable.

## Change policy

Essay editing and reconciliation are deliberately different:

- **Prohibited:** changing an essay's argument or wording, established route identity, typography, layout, colors, post-list design, header/footer behavior, or current PaperMod light/dark behavior.
- **Potentially approvable reconciliation:** correcting front matter or a citation destination while preserving the argument and design. Approval is not implied merely by regenerating a hash.

Before changing the baseline for a reconciliation, add an entry to `tests/baselines/review-records.json` containing:

1. a unique record ID and `kind` (`front-matter-reconciliation` or `citation-reconciliation`);
2. the exact article and field/link before and after;
3. why the old value was defective;
4. verification evidence for the corrected value;
5. confirmation that prose, argument, route, and presentation remain unchanged.

Then update only the affected explicit value and digest in `preservation.json`, run the full validator, and include both files in review. Never update baseline hashes to make an unexplained content or design change pass. The initial record identifies the inherited commit used for this inventory and approves no changes.
