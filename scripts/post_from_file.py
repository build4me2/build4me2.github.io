#!/usr/bin/env python3
"""Convert a PDF or TXT paper into a Hugo blog post.

Format rules:
- Removes common cover/header metadata from the body.
- Keeps the paper's own words and paragraph order.
- Produces TOML front matter with no TOC/meta/summary.
- Converts raw URLs into inline links with the visible URL unchanged.
- Allows explicit inline citation links via --link TEXT=URL.

Usage examples:
  scripts/post_from_file.py ~/paper.pdf --title "My Paper Title"
  scripts/post_from_file.py ~/paper.txt --title "My Paper Title" --date 2026-06-02
  scripts/post_from_file.py ~/paper.pdf --title "My Paper" \
    --link 'Quoted text=https://example.com/source'
"""
from __future__ import annotations

import argparse
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BLOG_ROOT = Path(__file__).resolve().parents[1]
POSTS_DIR = BLOG_ROOT / "content" / "posts"

COMMON_HEADER_PATTERNS = [
    r"^Manisha Chand$",
    r"^Chand Manisha$",
    r"^CSC 300GW: Ethics, Communication, and Tools for Software Development$",
    r"^Dr\. Sanika Doolani$",
    r"^(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}$",
]


def slugify(title: str) -> str:
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug or "post"


def read_input(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        try:
            return subprocess.check_output(["pdftotext", str(path), "-"], text=True, errors="ignore")
        except FileNotFoundError:
            raise SystemExit("Error: pdftotext is required. Install poppler-utils.")
    return path.read_text(errors="ignore")


def clean_text(raw: str, title: str) -> str:
    raw = raw.replace("\u200b", "").replace("\ufeff", "").replace("\x0c", "\n\n")
    lines = [line.strip() for line in raw.splitlines()]

    filtered: list[str] = []
    for line in lines:
        if any(re.fullmatch(pattern, line) for pattern in COMMON_HEADER_PATTERNS):
            continue
        filtered.append(line.strip('"'))

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in filtered:
        if not line:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)

    paragraphs: list[str] = []
    for block in blocks:
        # Drop standalone page/footnote numbers.
        if len(block) == 1 and re.fullmatch(r"\d+", block[0]):
            continue
        paragraph = " ".join(block)
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        paragraph = paragraph.replace(" .", ".").replace(" ,", ",")
        paragraphs.append(paragraph)

    # Remove duplicate title/cover title at the beginning only.
    title_forms = {title, title.strip('"'), title.split(":")[0]}
    while paragraphs and paragraphs[0].strip('"') in title_forms:
        paragraphs.pop(0)

    return "\n\n".join(paragraphs).strip()


def apply_links(text: str, explicit_links: list[str]) -> str:
    # First apply requested TEXT=URL links.
    for item in explicit_links:
        if "=" not in item:
            raise SystemExit(f"Bad --link value: {item!r}. Use TEXT=URL")
        label, url = item.split("=", 1)
        if label in text:
            text = text.replace(label, f'<a href="{url}">{label}</a>', 1)

    # Then convert raw URLs into links without changing visible URL text.
    def repl(match: re.Match[str]) -> str:
        url = match.group(0)
        # Avoid touching URLs already inside href attributes or markdown links.
        before = text[max(0, match.start() - 8):match.start()]
        if 'href="' in before or "](" in before:
            return url
        clean = url.rstrip(".;,")
        trail = url[len(clean):]
        return f'<a href="{clean}">{clean}</a>{trail}'

    return re.sub(r"https?://[^\s)]+", repl, text)


def front_matter(title: str, date: str, slug: str) -> str:
    return f'''+++\ntitle = "{title.replace('"', '\\"')}"\ndate = {date}\ndraft = false\nslug = "{slug}"\nhideSummary = true\nhideMeta = true\nShowToc = false\n+++\n\n'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a Hugo post from a PDF or TXT file.")
    parser.add_argument("input", type=Path, help="PDF or TXT file")
    parser.add_argument("--title", required=True, help="Post title")
    parser.add_argument("--date", help="Post date, e.g. 2026-06-02 or 2026-06-02T09:00:00-07:00")
    parser.add_argument("--slug", help="URL slug; defaults to slugified title")
    parser.add_argument("--link", action="append", default=[], help="Embed citation link: TEXT=URL. Can repeat.")
    parser.add_argument("--output", type=Path, help="Output .md path; defaults to content/posts/<slug>.md")
    args = parser.parse_args()

    slug = args.slug or slugify(args.title)
    date = args.date or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date += "T09:00:00-07:00"

    raw = read_input(args.input)
    body = clean_text(raw, args.title)
    body = apply_links(body, args.link)

    output = args.output or POSTS_DIR / f"{slug}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(front_matter(args.title, date, slug) + body + "\n")
    print(f"Wrote {output}")
    print("Next: run `hugo --minify`, review the post, then git add/commit/push.")


if __name__ == "__main__":
    main()
