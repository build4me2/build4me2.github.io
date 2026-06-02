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

    while blocks and (blocks[0].strip('"') in {title, title_short} or blocks[0].startswith(title_short)):
        blocks.pop(0)

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
    text = re.sub(r"([.!?”])\s*([1-9])(?=\s|$)", r"\1", text)
    text = re.sub(r"([A-Za-z”\)])([1-9])(?=\s)", r"\1", text)

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


def front_matter(title: str, date: str, slug: str) -> str:
    safe_title = title.replace('"', '\\"')
    return f'''+++\ntitle = "{safe_title}"\ndate = {date}\ndraft = false\nslug = "{slug}"\nhideSummary = true\nhideMeta = true\nShowToc = false\n+++\n\n'''


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

    body = parse_body(read_input(args.input), args.title, args.keep_references)
    body = fix_extraction_artifacts(body)
    body = embed_links(body, args.link)
    body = "\n\n".join(p.strip() for p in body.split("\n\n") if p.strip())

    output = args.output or POSTS_DIR / f"{slug}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(front_matter(args.title, date, slug) + body + "\n")
    print(f"Wrote {output}")
    print("Review, then run: hugo --minify && git add/commit/push")


if __name__ == "__main__":
    main()
