# Blog post conversion script

Use `scripts/post_from_file.py` to convert a `.pdf` or `.txt` file into a Hugo Markdown post using the blog's current format: cover/header removed, clean body paragraphs preserved, citation numbers removed after inline links are embedded.

Blog rule: **every cited sentence must carry its source link embedded inline.** PDF hyperlinks are annotations that plain text extraction drops, so the script now recovers them itself:

1. It reads every hyperlink annotation in the PDF (via `pdftohtml`).
2. Links anchored on numbered footnotes are embedded automatically on the sentence that carries that footnote marker in the body.
3. Before any output is staged, it cross-checks every recovered PDF citation destination. Any destination still missing from the post blocks the transaction with `ERROR[missing_citations]`; no post or temporary file is created. For APA author-year papers (no footnote numbers), add each source with `--link 'cited text=URL'` and retry.

### Deterministic citation candidate recovery

Citation review tooling can call `extract_citation_candidates(raw_text, pdf_links,
source=...)`. It recovers each explicit PDF annotation, visible HTTP(S) URL,
conservatively wrapped line/page URL, DOI resolver URL, `doi:` identifier, and
bare DOI from both body and reference text. Each immutable candidate retains its
raw evidence, normalized destination, extraction method, source/page/line/section
location, and provenance; `to_dict()` provides a JSON-ready record. Ordering is
annotation order followed by text order and the extractor never performs network
requests. Normalization is limited to URL scheme/host casing, HTML entity
unescaping, clear surrounding punctuation, and DOI case/canonical resolver form.
It does not infer destinations from author names, titles, domains without a URL
scheme, or other citation prose. Repeated occurrences are retained rather than
silently deduplicated because duplicate evidence must be reviewed.

### Deterministic matching records

`parse_citation_records(raw_text, source=...)` assigns source-order IDs to body
sentences, bracketed/trailing numeric markers, parenthesized author-year forms,
inline Markdown/HTML links, and numbered or author-year reference entries.
`match_citation_candidates(raw_text, candidates, source=...)` gives every parsed
record and candidate a disposition. Its rules are intentionally narrow:

- numeric and author-year citations match only one exactly identical reference
  identity (`numeric:N` or normalized first-surname/year, including year suffix);
- inline links match only the same normalized candidate destination on the same
  source page and line;
- reference destinations match only candidates extracted on that entry's source
  lines; and
- source order determines IDs and report order, but is never used to break a tie.

No titles, similarity, nearby entries, or network metadata participate. Zero
matches are `unresolved`, while multiple exact matches are `ambiguous`; neither
selects a target. Duplicate reference/citation identities, duplicate candidate
destinations, unlinked/malformed references, orphan candidates, and references
with conflicting destinations receive explicit blocking statuses. A sentence
with citations inherits any blocking citation state. `CitationMatchResult.blocking`
is therefore true whenever any disposition is not `matched` (uncited sentences
are explicitly `matched` with a no-citation explanation).

### Fail-closed audit reports

Ingestion runs deterministic matching before the atomic post installation. Any
unresolved, unlinked, ambiguous, duplicate, malformed, orphaned, suspicious, or
conflicting disposition exits with `ERROR[citation_audit]` and the destination is
never created. URLs containing credentials or targeting localhost are treated as
suspicious; explicit HTTP(S) tokens rejected by normalization are reported as
malformed rather than silently disappearing.

Every CLI invocation emits two views of its audit, including failures before
citation extraction: canonical JSON on standard output and a human-readable
report on standard error. The JSON uses `citation-audit-report/v1` and includes a
content hash (or the explicit `unavailable` sentinel when no input could safely
be opened), source name, source-order records and evidence, provenance,
dispositions, exact override consumption, errors, and summary/status counts.
The text view carries the same evidence and decisions. Reports contain no clock,
random, host, temporary-directory, or absolute-path values and are stable across
identical invocations. `scripts.citation_audit.build_audit_report()`,
`render_json()`, and `render_text()` expose the same report functionality to
repository validators without network access.

### Reviewed citation overrides

The committed `citation-overrides.json` file is the only review-override format.
Its machine-readable schema is `scripts/citation-overrides.schema.json`; runtime
validation is intentionally stricter than generic JSON Schema validation (it also
rejects duplicate JSON members, duplicate/conflicting targets, non-normalized
URLs, wildcard identities, and control characters).

An override is keyed by `documentIdentity` (the `sha256:` value returned by
`scripts.citation_overrides.document_identity`) and one exact semantic
`citationIdentity`, such as `numeric:7` or `author-year:smith:2020`. Each record
requires a unique stable ID, the reviewer, explicit
`resolve-citation-destination` intent, one exact normalized HTTP(S) destination,
a rationale, and exactly one of `evidenceText` or `evidenceSource`. Evidence text
must still occur in the cited sentence when the override is consumed. Never use
globs, URL patterns, or generic waiver language.

`load_citation_overrides()` validates a file and
`apply_citation_overrides()` applies it to a complete mapping of document hashes
to `CitationMatchResult` objects. Every record must be consumed exactly once by
one currently blocking citation and one uniquely orphaned recovered candidate.
A missing/changed document, absent or ambiguous citation identity, already-fixed
citation, duplicate destination candidate, stale evidence, or candidate already
owned by another record fails validation. Application changes only that citation,
that candidate, and their owning sentence; unrelated unresolved, malformed,
ambiguous, duplicate, or conflicting records remain blocking. Thus an override
cannot act as a broad waiver or make an otherwise defective audit publishable.
The returned `OverrideUse` records provide the exact citation/candidate pairing
for human-readable and JSON audit reports.

## Safety contract

The command accepts only regular, non-symlink `.pdf` and UTF-8 `.txt` files of
at most 25 MiB. The extension must match the file contents. Titles must be
non-empty, strictly UTF-8-representable, and contain no C0, DEL, or C1 control
characters; dates must be valid
ISO 8601 dates (or timezone-qualified timestamps), and slugs use only lowercase
ASCII letters, digits, and single hyphens. The complete generated front matter
is parsed as TOML before anonymous staging begins, so an escaping or generation
defect cannot install a malformed post. Each
repeatable `--link 'TEXT=URL'` mapping is parsed before extraction: its label must
be non-empty and unique, and its destination must be an absolute HTTP(S) URL with
a fully qualified public host, no credentials, whitespace, controls, malformed
percent escapes, percent-encoded hostnames, or HTML/quote delimiters. Legacy
numeric IPv4 spellings (including shortened, octal, hexadecimal, and integer
forms) are rejected so
loopback or private destinations cannot masquerade as DNS hosts. Scheme and host
case, IDN hosts, default ports, and an empty path are normalized deterministically. After
extraction every label must occur exactly once; absent, repeated, duplicate, or
overlapping labels block publication. Valid destinations are HTML-escaped before
attribute insertion, and matching is done before generated anchors exist, so a
mapping cannot match or alter an HTML attribute. Explicit `--output` paths must
name a new `.md` file below this checkout's
`content/posts/` tree; output directories are never created and existing posts
are never overwritten. Validation failures use stable `ERROR[category]: ...`
diagnostics, return nonzero, and do not create an output file. After all
extraction and citation checks pass, the complete post is written to an anonymous
`O_TMPFILE` inode in the destination directory, flushed and verified byte-for-byte,
then given the destination name atomically without replacement. The staging inode
never has a pathname, so pre-install failures require no directory cleanup and
cannot leave temporary debris. Input bytes come from the regular
file descriptor opened during validation. For PDFs, the captured bytes are copied
to a temporary descriptor whose pathname is immediately unlinked; every Poppler
process inherits that same descriptor and opens it through `/proc/self/fd`, so no
mutable snapshot name exists and replacing the input pathname cannot change any
extraction pass. The snapshot is closed before publication. A lifecycle failure
uses `ERROR[snapshot_cleanup]` and blocks publication, while cleanup can never
replace an earlier extraction diagnostic. Output
parents are opened component-by-component without following links and retained
through installation; anonymous staging and verification are performed relative
to that bound directory descriptor. At the commit boundary, the posts root and
complete no-follow parent chain are reopened and required to retain their
validated device/inode identities and containment before installation. The same
chain is rebound again immediately after `linkat` and must still identify both
the validated parent and the newly installed staging inode. The retained staging
descriptor must then report exactly one directory link, proving that the returned
canonical path is the generated inode's only name. Linux has no single
conditional link operation that compares a directory inode with its pathname;
if relocation occurs inside that final syscall window, the converter retracts
only its own inode through the retained directory descriptor before reporting
`ERROR[unsafe_output]`. Retraction is verified by requiring a zero link count.
If the high-level unlink path fails or is ineffective, rollback uses an
independent `unlinkat` syscall against the retained verified directory descriptor
and proves the staging inode has zero links before returning the original
verification failure. A rollback fault is reported as `ERROR[output_rollback]`
only when that proof cannot be established; it is never converted into success.
Any concurrently created canonical destination remains unchanged byte-for-byte.
Controlled fixtures cover ordinary retraction, parent relocation outside and
inside the posts tree, concurrent canonical creation, post-link verification
faults, intercepted unlink calls, and ineffective cleanup calls.

## Extraction failure policy

TXT input is decoded as strict UTF-8 (an optional UTF-8 BOM is removed). The
converter never uses the system locale, guesses another encoding, or replaces
invalid bytes. UTF-16/UTF-32 and other encodings, malformed UTF-8, and NUL bytes
therefore fail with `ERROR[encoding_error]`.

PDF ingestion requires `pdfinfo`, `pdftotext`, and `pdftohtml` from
`poppler-utils`. Each executable is checked explicitly and each invocation runs
in a new process group with a 30-second timeout. Standard output and standard
error are drained concurrently throughout execution into fixed-size buffers (64
MiB and 64 KiB respectively), so a flooding or mutually blocked tool cannot grow
converter memory without bound or fill one pipe while the other is read. Exceeding
either cap fails explicitly with `ERROR[tool_output_limit]`. On timeout or output
overflow the complete group (including tool-spawned descendants) receives a
bounded graceful termination interval followed by a forced kill and bounded reap;
cleanup never waits indefinitely on a descendant holding an output pipe. A missing executable,
nonzero exit (including corrupt/encrypted PDFs), timeout, non-UTF-8 tool output,
empty text, page-separator counts that disagree with `pdfinfo`, or any missing,
empty, or implausibly sparse individual page (regardless of aggregate text length),
URL annotations reported by `pdfinfo` but absent from `pdftohtml`, or a recovered citation
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
