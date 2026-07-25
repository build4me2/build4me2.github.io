#!/usr/bin/env python3
"""Convert a PDF or TXT paper into a Hugo blog post.

Blog format rules:
- Remove only the cover/header metadata from the post body.
- Preserve the paper body words and paragraph order.
- Join PDF-wrapped lines into clean paragraphs.
- Do not split paragraphs at PDF page breaks or footnote blocks.
- Remove numbered citation markers when replacing them with embedded links.
- No separate citation section unless you intentionally keep one.

Examples:
  scripts/post_from_file.py paper.pdf --title "My Paper Title"
  scripts/post_from_file.py paper.pdf --title "My Paper" --date 2026-06-02
  scripts/post_from_file.py paper.pdf --title "My Paper" \
    --link 'quoted/cited text=https://example.com/source'
"""
from __future__ import annotations

import argparse
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BLOG_ROOT = Path(__file__).resolve().parents[1]
POSTS_DIR = BLOG_ROOT / "content" / "posts"

HEADER_PATTERNS = [
    r"^Manisha Chand$",
    r"^Chand Manisha$",
    r"^CSC 300GW:",
    r"^Dr\. Sanika Doolani$",
    r"^(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}$",
]


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "post"


def read_input(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        try:
            # -layout helps detect paragraph/page-footnote structure.
            return subprocess.check_output(["pdftotext", "-layout", str(path), "-"], text=True, errors="ignore")
        except FileNotFoundError:
            raise SystemExit("Error: pdftotext is required. Install poppler-utils.")
    return path.read_text(errors="ignore")


def extract_pdf_links(path: Path) -> list[tuple[str, str]]:
    """Return (anchor_text, url) pairs for every hyperlink annotation in the PDF.

    pdftotext drops hyperlinks entirely (they are annotations, not text), so the
    sources cited in the paper are lost unless we pull them out here.
    """
    if path.suffix.lower() != ".pdf":
        return []
    try:
        html = subprocess.check_output(
            ["pdftohtml", "-stdout", "-i", "-noframes", str(path)], text=True, errors="ignore",
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("WARNING: pdftohtml not found (install poppler-utils); cannot recover PDF hyperlinks.")
        return []
    import html as html_mod

    links: list[tuple[str, str]] = []
    for match in re.finditer(r'<a href="(https?://[^"]*)">(.*?)</a>', html, re.S):
        label = html_mod.unescape(re.sub(r"<[^>]+>", "", match.group(2)))
        label = re.sub(r"\s+", " ", label).replace("​", "").strip()
        if label:
            links.append((label, match.group(1)))
    return links


def footnote_url_map(pdf_links: list[tuple[str, str]], raw_text: str) -> dict[int, str]:
    """Map footnote number -> URL for links anchored on numbered footnote entries."""
    mapping: dict[int, str] = {}
    unnumbered: list[tuple[str, str]] = []
    for label, url in pdf_links:
        match = re.match(r"^(\d{1,2})\s*\S", label)
        if match:
            mapping.setdefault(int(match.group(1)), url)
        else:
            unnumbered.append((label, url))

    # Some PDFs put the footnote number on its own line or anchor the link on the
    # reference text / raw URL instead of the number. Recover the number by parsing
    # footnote blocks from the raw text: a line that is (or starts with) a footnote
    # number, plus its continuation lines until a blank line.
    clean_lines = [line.strip() for line in raw_text.replace("​", "").splitlines()]
    blocks: list[tuple[int, str]] = []
    current_num, current_text = None, []
    for line in clean_lines:
        start = re.match(r"^(\d{1,2})\s*$|^(\d{1,2})\s+\S", line)
        if start:
            if current_num is not None:
                blocks.append((current_num, " ".join(current_text)))
            current_num = int(start.group(1) or start.group(2))
            current_text = [line]
        elif current_num is not None:
            if not line:
                blocks.append((current_num, " ".join(current_text)))
                current_num, current_text = None, []
            else:
                current_text.append(line)
    if current_num is not None:
        blocks.append((current_num, " ".join(current_text)))

    for label, url in unnumbered:
        head = label[:30].strip()
        for num, block in blocks:
            if head and (head in block or url.rstrip("/") in block.replace(" ", "")):
                mapping.setdefault(num, url)
                break
    return mapping


def embed_footnote_links(text: str, notes: dict[int, str]) -> str:
    """Wrap the sentence ending at footnote marker N with a link to footnote N's URL."""
    for num, url in sorted(notes.items()):
        marker = re.search(r"(?<!\d)([.!?][”\"\')]*)" + str(num) + r"(?=\s|$)", text)
        if not marker:
            continue
        end = marker.end(1)
        # The sentence starts after the previous sentence end (or paragraph start).
        prev = max(
            text.rfind(". ", 0, marker.start()),
            text.rfind("? ", 0, marker.start()),
            text.rfind("! ", 0, marker.start()),
            text.rfind("\n\n", 0, marker.start()),
            text.rfind("</a>", 0, marker.start()),
        )
        start = 0 if prev == -1 else prev + 2 if not text.startswith("</a>", prev) else prev + 4
        while start < end and text[start] in " \n":
            start += 1
        sentence = text[start:end]
        text = text[:start] + f'<a href="{url}">{sentence}</a>' + text[end:]
    return text


def parse_body(raw: str, title: str, keep_references: bool) -> str:
    raw = raw.replace("\u200b", "").replace("\ufeff", "").replace("\x0c", "\n\n")
    lines = [line.strip() for line in raw.splitlines()]
    title_short = title.split(":")[0].strip('"')

    cleaned: list[str] = []
    skipping_footnote = False
    for line in lines:
        if any(re.search(pattern, line) for pattern in HEADER_PATTERNS):
            continue
        if line.strip('"') in {title, title_short, title.lower(), title_short.lower()}:
            continue
        if title_short and line.startswith(title_short) and len(line) < len(title_short) + 15:
            continue

        # Drop footnote/citation blocks produced by PDF extraction. Use --link to embed them inline.
        if re.fullmatch(r"\d+", line):
            skipping_footnote = True
            continue
        if skipping_footnote:
            if not line:
                skipping_footnote = False
            continue
        cleaned.append(line)

    blocks: list[str] = []
    current: list[str] = []
    for line in cleaned:
        if not line:
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(" ".join(current))

    blocks = [re.sub(r"\s+", " ", block).strip().strip('"') for block in blocks]
    blocks = [block.replace(" .", ".").replace(" ,", ",") for block in blocks if block]

    # Drop leading blocks that merely repeat the post title (the theme already
    # renders the title as the page heading), comparing loosely so quoted or
    # re-punctuated variants of the title are caught too.
    def normalized(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    while blocks and (
        blocks[0].strip('"') in {title, title_short}
        or blocks[0].startswith(title_short)
        or normalized(blocks[0]) == normalized(title)
    ):
        blocks.pop(0)

    # Collapse accidental consecutive duplicate paragraphs from PDF extraction.
    deduped: list[str] = []
    for block in blocks:
        if deduped and normalized(block) == normalized(deduped[-1]):
            continue
        deduped.append(block)
    blocks = deduped

    paragraphs: list[str] = []
    for block in blocks:
        if not paragraphs:
            paragraphs.append(block)
            continue
        previous = paragraphs[-1]
        # If a PDF footnote/page break split a sentence, rejoin it.
        if (not re.search(r"[.!?][”\"\)]?$", previous)) or re.match(r"^[a-z]", block):
            paragraphs[-1] = previous + " " + block
        else:
            paragraphs.append(block)

    text = "\n\n".join(paragraphs).strip()
    if not keep_references:
        text = re.sub(r"\n\nReferences\s+.*$", "", text, flags=re.S)
    return text


def fix_extraction_artifacts(text: str) -> str:
    # Fix only PDF extraction glitches, not the author's wording.
    fixes = {
        "Thebounds": "The bounds",
        "utilitarianpredictionabout": "utilitarian prediction about",
        "softwarewill": "software will",
        "itshouldbe": "it should be",
        "andit": "and it",
        "Class ,": "Class,",
    }
    for bad, good in fixes.items():
        text = text.replace(bad, good)
    return text


def embed_links(text: str, explicit_links: list[str]) -> str:
    for item in explicit_links:
        if "=" not in item:
            raise SystemExit(f"Bad --link value: {item!r}. Use TEXT=URL")
        label, url = item.split("=", 1)
        if label in text:
            text = text.replace(label, f'<a href="{url}">{label}</a>', 1)

    # Remove numeric citation markers after embedding links.
    # Example PDF text: cited sentence.3
    # If --link wrapped the cited sentence, it becomes <a ...>cited sentence.</a>3;
    # remove that trailing citation number too.
    text = re.sub(r"(</a>)\s*([1-9]|1[0-9])(?=\s|$)", r"\1", text)
    # (?<!\d) protects decimals: "9.7 gigabytes" must not lose its 7.
    text = re.sub(r"(?<!\d)([.!?”])\s*([1-9]|1[0-9])(?=\s|$)", r"\1", text)
    text = re.sub(r"([A-Za-z”\)])([1-9]|1[0-9])(?=\s)", r"\1", text)

    # Convert raw URLs into links without changing visible URL text.
    def repl(match: re.Match[str]) -> str:
        url = match.group(0)
        before = text[max(0, match.start() - 10):match.start()]
        if 'href="' in before:
            return url
        clean = url.rstrip(".;,")
        trail = url[len(clean):]
        return f'<a href="{clean}">{clean}</a>{trail}'

    return re.sub(r"(?<![\"=])https?://[^\s)]+", repl, text)


def check_all_sources_embedded(text: str, pdf_links: list[tuple[str, str]]) -> None:
    """Fail loudly when a source hyperlink from the PDF is missing from the post."""
    missing = [(label, url) for label, url in pdf_links if url not in text]
    if not missing:
        return
    print("\nWARNING: these source links exist in the PDF but are NOT embedded in the post.")
    print("Every cited sentence must carry its source link. Re-run adding for each one:")
    for label, url in missing:
        print(f"  --link 'exact cited text from the body={url}'   # source: {label[:80]}")
    print()


def front_matter(title: str, date: str, slug: str) -> str:
    safe_title = title.replace('"', '\\"')
    return f'''+++\ntitle = "{safe_title}"\ndate = {date}\ndraft = false\nslug = "{slug}"\nhideSummary = true\nShowToc = false\n+++\n\n'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a Hugo post from a PDF or TXT file.")
    parser.add_argument("input", type=Path, help="PDF or TXT file")
    parser.add_argument("--title", required=True, help="Post title")
    parser.add_argument("--date", help="Post date, e.g. 2026-06-02 or 2026-06-02T09:00:00-07:00")
    parser.add_argument("--slug", help="URL slug; defaults to slugified title")
    parser.add_argument("--link", action="append", default=[], help="Embed citation link: TEXT=URL. Can repeat.")
    parser.add_argument("--keep-references", action="store_true", help="Keep a final References section if present")
    parser.add_argument("--output", type=Path, help="Output .md path; defaults to content/posts/<slug>.md")
    args = parser.parse_args()

    slug = args.slug or slugify(args.title)
    date = args.date or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date += "T09:00:00-07:00"

    pdf_links = extract_pdf_links(args.input)
    raw_text = read_input(args.input)
    body = parse_body(raw_text, args.title, args.keep_references)
    body = fix_extraction_artifacts(body)
    body = embed_footnote_links(body, footnote_url_map(pdf_links, raw_text))
    body = embed_links(body, args.link)
    body = "\n\n".join(p.strip() for p in body.split("\n\n") if p.strip())
    check_all_sources_embedded(body, pdf_links)

    output = args.output or POSTS_DIR / f"{slug}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(front_matter(args.title, date, slug) + body + "\n")
    print(f"Wrote {output}")
    print("Review, then run: hugo --minify && git add/commit/push")


if __name__ == "__main__":
    main()
