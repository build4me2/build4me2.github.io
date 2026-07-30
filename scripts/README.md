# Blog post conversion script

Use `scripts/post_from_file.py` to convert a `.pdf` or `.txt` file into a Hugo Markdown post using the blog's current format: cover/header removed, clean body paragraphs preserved, citation numbers removed after inline links are embedded.

Blog rule: **every cited sentence must carry its source link embedded inline.** PDF hyperlinks are annotations that plain text extraction drops, so the script now recovers them itself:

1. It reads every hyperlink annotation in the PDF (via `pdftohtml`).
2. Links anchored on numbered footnotes are embedded automatically on the sentence that carries that footnote marker in the body.
3. At the end it cross-checks: any PDF source link still missing from the post is printed as a WARNING with a ready-made `--link` flag to copy. Do not publish a post while that warning lists missing sources — for APA author-year papers (no footnote numbers), add each source with `--link 'cited text=URL'`.

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

After creating a post, use the repository-pinned Hugo Extended 0.162.0 toolchain (see `docs/preservation-baselines.md`):

```bash
python3 scripts/validate_preservation_baseline.py
hugo --minify
git add content/posts/<slug>.md
git commit -m "Add blog post"
git push origin main
```
