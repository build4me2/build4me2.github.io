# Blog post conversion script

Use `scripts/post_from_file.py` to convert a `.pdf` or `.txt` file into a Hugo Markdown post using the blog's current format: cover/header removed, clean body paragraphs preserved, citation numbers removed after inline links are embedded.

Blog rule: **every cited sentence must carry its source link embedded inline.** PDF hyperlinks are annotations that plain text extraction drops, so the script now recovers them itself:

1. It reads every hyperlink annotation in the PDF (via `pdftohtml`).
2. Links anchored on numbered footnotes are embedded automatically on the sentence that carries that footnote marker in the body.
3. Before any output is staged, it cross-checks every recovered PDF citation destination. Any destination still missing from the post blocks the transaction with `ERROR[missing_citations]`; no post or temporary file is created. For APA author-year papers (no footnote numbers), add each source with `--link 'cited text=URL'` and retry.

## Safety contract

The command accepts only regular, non-symlink `.pdf` and UTF-8 `.txt` files of
at most 25 MiB. The extension must match the file contents. Titles must be
non-empty, dates must be valid ISO 8601 dates (or timezone-qualified timestamps),
and slugs use only lowercase ASCII letters, digits, and single hyphens. Explicit
`--output` paths must name a new `.md` file below this checkout's
`content/posts/` tree; output directories are never created and existing posts
are never overwritten. Validation failures use stable `ERROR[category]: ...`
diagnostics, return nonzero, and do not create an output file. After all
extraction and citation checks pass, the complete post is written to a hidden
temporary file in the destination directory, flushed and verified byte-for-byte,
then atomically installed without replacement. Input bytes come from the regular
file descriptor opened during validation (PDF tools share one private validated
snapshot), so replacing the input pathname cannot change extraction. Output
parents are opened component-by-component without following links and retained
through installation; staging, verification, linking, and cleanup are all
performed relative to that bound directory descriptor. Any failure removes the
staging file; a destination created concurrently or already present remains
unchanged, and replacing an output parent pathname cannot redirect the post.

## Extraction failure policy

TXT input is decoded as strict UTF-8 (an optional UTF-8 BOM is removed). The
converter never uses the system locale, guesses another encoding, or replaces
invalid bytes. UTF-16/UTF-32 and other encodings, malformed UTF-8, and NUL bytes
therefore fail with `ERROR[encoding_error]`.

PDF ingestion requires `pdfinfo`, `pdftotext`, and `pdftohtml` from
`poppler-utils`. Each executable is checked explicitly and each invocation has
a 30-second timeout with captured, bounded diagnostics. A missing executable,
nonzero exit (including corrupt/encrypted PDFs), timeout, non-UTF-8 tool output,
empty text, an implausibly sparse/truncated page extraction, URL annotations
reported by `pdfinfo` but absent from `pdftohtml`, or a recovered citation
destination that is not embedded in the converted body is a blocking error.
These checks happen before the destination is created; do not bypass them—OCR,
repair the source document, or supply exact `--link` mappings instead.

Examples:

```bash
scripts/post_from_file.py "/path/to/paper.pdf" --title "My Paper Title"
```

With date and citation embedding:

```bash
scripts/post_from_file.py "/path/to/paper.pdf" \
  --title "Ethical Frameworks for Development in Technology" \
  --date 2026-02-09 \
  --link '“Technologies can grow very quickly and outpace politics – innovative companies often find themselves in a gray legal zone. Once enough people depend on the technology, shutting it down becomes politically untenable — the politics gets dragged along.”=https://conversationswithbillkristol.org/transcript/peter-thiel-transcript/'
```

Important: use `--link 'exact cited text=URL'` for each numbered citation you want embedded. The visible cited text stays the same; it only becomes underlined/clickable. If the PDF has a citation marker immediately after that text, like `...positive outcome in that regard.3`, the script removes the trailing number after embedding the link.

The default destination is `content/posts/<slug>.md`. If a deliberate retry
needs the same slug, review and move/remove the old post yourself; the converter
will not silently replace it.

After creating a post, use the repository-pinned Hugo Extended 0.162.0 toolchain (see `docs/preservation-baselines.md` and `docs/reproducible-builds.md`):

```bash
make setup # deterministic offline PaperMod setup
make validate
make reproducible
make build # always replaces public/ with a clean deterministic build
make verify-routes
git add content/posts/<slug>.md
git commit -m "Add blog post"
git push origin main
```
