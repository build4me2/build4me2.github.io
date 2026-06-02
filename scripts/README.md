# Blog post conversion script

Use `scripts/post_from_file.py` to convert a `.pdf` or `.txt` file into a Hugo Markdown post using the blog's current format: cover/header removed, clean body paragraphs preserved, citation numbers removed after inline links are embedded.

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

Important: use `--link 'exact cited text=URL'` for each numbered citation you want embedded. The visible cited text stays the same; it only becomes underlined/clickable.

After creating a post:

```bash
hugo --minify
git add content/posts/<slug>.md
git commit -m "Add blog post"
git push origin main
```
