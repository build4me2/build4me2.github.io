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
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

BLOG_ROOT = Path(__file__).resolve().parents[1]
POSTS_DIR = BLOG_ROOT / "content" / "posts"
SUPPORTED_INPUT_SUFFIXES = frozenset({".pdf", ".txt"})
MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_TITLE_LENGTH = 300
MAX_SLUG_LENGTH = 100
PDF_TOOL_TIMEOUT_SECONDS = 30
MAX_TOOL_DIAGNOSTIC_CHARS = 1000
MIN_PDF_TEXT_CHARACTERS_PER_PAGE = 20
SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class IngestionError(Exception):
    """An expected, categorized command failure safe to show to a user."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class IngestionArgumentParser(argparse.ArgumentParser):
    """Turn argparse failures into the CLI's stable diagnostic format."""

    def error(self, message: str) -> None:
        raise IngestionError("usage", message)

HEADER_PATTERNS = [
    r"^Manisha Chand$",
    r"^Chand Manisha$",
    r"^CSC 300GW:",
    r"^Dr\. Sanika Doolani$",
    r"^(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}$",
]


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _has_parent_traversal(path: Path) -> bool:
    return ".." in path.parts


def _contains_symlink(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current /= part
        if current.is_symlink():
            return True
    return False


class InputSnapshot:
    """Bytes read from one validated open input object, never from its name again."""

    def __init__(self, path: Path, suffix: str, data: bytes, descriptor: int) -> None:
        self.path = path
        self.suffix = suffix
        self.data = data
        self.descriptor = descriptor
        self._snapshot_path: Path | None = None

    def __eq__(self, other: object) -> bool:
        # Preserve the small public helper's historical convenience in callers.
        return self.path == other

    def pdf_path(self) -> Path:
        if self._snapshot_path is None:
            try:
                descriptor, name = tempfile.mkstemp(prefix="hugo-ingestion-", suffix=".pdf")
                try:
                    with os.fdopen(descriptor, "wb") as destination:
                        destination.write(self.data)
                        destination.flush()
                        os.fsync(destination.fileno())
                except BaseException:
                    Path(name).unlink(missing_ok=True)
                    raise
            except OSError as exc:
                raise IngestionError(
                    "extraction", f"cannot create validated PDF snapshot: {exc.strerror or exc}"
                ) from None
            self._snapshot_path = Path(name)
        return self._snapshot_path

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1
        if self._snapshot_path is not None:
            self._snapshot_path.unlink(missing_ok=True)
            self._snapshot_path = None

    def __del__(self) -> None:
        self.close()


def validate_input(path: Path, max_bytes: int = MAX_INPUT_BYTES) -> InputSnapshot:
    """Open, validate, and snapshot an input without trusting its name again."""
    if _has_parent_traversal(path):
        raise IngestionError("unsafe_input", "input path must not contain '..'")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_INPUT_SUFFIXES:
        expected = ", ".join(sorted(SUPPORTED_INPUT_SUFFIXES))
        raise IngestionError("unsupported_input", f"input extension must be one of: {expected}")

    try:
        info = path.lstat()
    except FileNotFoundError:
        raise IngestionError("missing_input", f"input does not exist: {path}") from None
    except OSError as exc:
        raise IngestionError("invalid_input", f"cannot inspect input: {exc.strerror or exc}") from None
    if stat.S_ISLNK(info.st_mode) or _contains_symlink(path.absolute()):
        raise IngestionError("unsafe_input", "input path must not contain symbolic links")
    if not stat.S_ISREG(info.st_mode):
        raise IngestionError("invalid_input", "input must be a regular file")
    if info.st_size > max_bytes:
        raise IngestionError(
            "input_too_large", f"input is {info.st_size} bytes; maximum is {max_bytes} bytes"
        )

    # Check the bytes as well as the name so renamed PDFs and obvious binary TXT
    # files cannot enter the wrong extraction path.
    fd = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            os.close(fd)
            raise IngestionError("unsafe_input", "input changed while it was being validated")
        with os.fdopen(os.dup(fd), "rb") as stream:
            head = stream.read(max_bytes + 1)
        after = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        final_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if identity != final_identity or len(head) != opened.st_size:
            os.close(fd)
            raise IngestionError("unsafe_input", "input changed while its validated snapshot was read")
    except IngestionError:
        raise
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        raise IngestionError("invalid_input", f"cannot read input: {exc.strerror or exc}") from None

    snapshot = InputSnapshot(path=path, suffix=suffix, data=head, descriptor=fd)
    try:
        is_pdf = head.startswith(b"%PDF-")
        if suffix == ".pdf" and not is_pdf:
            raise IngestionError("misleading_input", "a .pdf input must begin with a PDF signature")
        if suffix == ".txt":
            if is_pdf:
                raise IngestionError("misleading_input", "a PDF document must not use a .txt extension")
            if b"\x00" in head:
                raise IngestionError("encoding_error", "TXT input contains NUL bytes")
            try:
                head.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError as exc:
                raise IngestionError(
                    "encoding_error", f"TXT input is not strict UTF-8 at byte {exc.start}"
                ) from None
    except BaseException:
        snapshot.close()
        raise
    return snapshot


def validate_title(title: str) -> str:
    title = title.strip()
    if not title:
        raise IngestionError("invalid_title", "title must not be empty")
    if len(title) > MAX_TITLE_LENGTH:
        raise IngestionError("invalid_title", f"title must be at most {MAX_TITLE_LENGTH} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in title):
        raise IngestionError("invalid_title", "title must not contain control characters")
    return title


def validate_slug(slug: str) -> str:
    if not slug or len(slug) > MAX_SLUG_LENGTH or SLUG_PATTERN.fullmatch(slug) is None:
        raise IngestionError(
            "invalid_slug",
            f"slug must be 1-{MAX_SLUG_LENGTH} lowercase ASCII letters, digits, or single hyphens",
        )
    return slug


def validate_date(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise IngestionError("invalid_date", "date must be a valid ISO 8601 date or timestamp") from None
        return value + "T09:00:00-07:00"
    timestamp_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})"
    if re.fullmatch(timestamp_pattern, value) is None:
        raise IngestionError("invalid_date", "date must be a valid ISO 8601 date or timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise IngestionError("invalid_date", "date must be a valid ISO 8601 date or timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IngestionError("invalid_date", "timestamp must include a UTC offset")
    return value


class OutputTarget:
    """A destination name bound to its already-open, no-follow parent directory."""

    def __init__(self, path: Path, parent_descriptor: int, name: str) -> None:
        self.path = path
        self.parent_descriptor = parent_descriptor
        self.name = name

    def __eq__(self, other: object) -> bool:
        return self.path == other

    def close(self) -> None:
        if self.parent_descriptor >= 0:
            os.close(self.parent_descriptor)
            self.parent_descriptor = -1

    def __del__(self) -> None:
        self.close()


def _open_directory(path: Path, *, dir_fd: int | None = None) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, dir_fd=dir_fd)


def validate_output(path: Path, posts_dir: Path = POSTS_DIR) -> OutputTarget:
    """Bind a new Markdown name to a retained, no-follow parent directory."""
    if _has_parent_traversal(path):
        raise IngestionError("unsafe_output", "output path must not contain '..'")
    candidate = path if path.is_absolute() else Path.cwd() / path
    try:
        posts_root = posts_dir.resolve(strict=True)
    except OSError as exc:
        raise IngestionError("unsafe_output", f"configured posts directory is unavailable: {exc}") from None
    candidate = candidate.absolute()
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(posts_root)
    except ValueError:
        raise IngestionError("unsafe_output", f"output must be inside {posts_root}") from None
    if resolved == posts_root or candidate.suffix != ".md":
        raise IngestionError("unsafe_output", "output must be a .md file inside the posts directory")

    # Reject symlinked path components even if they happen to resolve back into
    # the posts tree. This keeps destination identity unambiguous.
    try:
        relative = candidate.relative_to(posts_root)
    except ValueError:
        raise IngestionError("unsafe_output", f"output must use a path below {posts_root}") from None
    descriptor = -1
    try:
        descriptor = _open_directory(posts_root)
        for part in relative.parts[:-1]:
            next_descriptor = _open_directory(Path(part), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        try:
            os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise IngestionError("output_exists", f"refusing to overwrite existing post: {candidate}")
    except IngestionError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise IngestionError(
            "unsafe_output", f"cannot bind output parent without following links: {exc.strerror or exc}"
        ) from None
    return OutputTarget(candidate, descriptor, relative.name)


def _diagnostic(stderr: bytes | str) -> str:
    """Return a bounded, single-line tool diagnostic that is safe for the CLI."""
    detail = stderr if isinstance(stderr, str) else stderr.decode("utf-8", errors="replace")
    detail = detail.replace("\x00", "�")
    detail = " ".join(detail.split())
    if not detail:
        return "no diagnostic output"
    if len(detail) > MAX_TOOL_DIAGNOSTIC_CHARS:
        return detail[:MAX_TOOL_DIAGNOSTIC_CHARS] + "…"
    return detail


def _run_pdf_tool(tool: str, arguments: list[str]) -> bytes:
    """Run one required Poppler tool with bounded resources and diagnostics."""
    executable = shutil.which(tool)
    if executable is None:
        raise IngestionError("missing_tool", f"required PDF tool '{tool}' was not found; install poppler-utils")
    try:
        result = subprocess.run(
            [executable, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=PDF_TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        detail = _diagnostic(exc.stderr or b"")
        raise IngestionError(
            "tool_timeout",
            f"{tool} exceeded the {PDF_TOOL_TIMEOUT_SECONDS}-second timeout ({detail})",
        ) from None
    except OSError as exc:
        raise IngestionError("tool_failed", f"could not execute {tool}: {exc.strerror or exc}") from None
    if result.returncode != 0:
        raise IngestionError(
            "tool_failed",
            f"{tool} exited with status {result.returncode}: {_diagnostic(result.stderr)}",
        )
    return result.stdout


def _decode_tool_output(tool: str, output: bytes) -> str:
    try:
        return output.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise IngestionError(
            "encoding_error", f"{tool} produced non-UTF-8 output at byte {exc.start}"
        ) from None


def _pdf_page_count(path: Path) -> int:
    try:
        raw_info = _run_pdf_tool("pdfinfo", ["-enc", "UTF-8", str(path)])
    except IngestionError as exc:
        if exc.category == "tool_failed":
            raise IngestionError("corrupt_pdf", f"pdfinfo could not read the PDF: {exc}") from None
        raise
    info = _decode_tool_output("pdfinfo", raw_info)
    match = re.search(r"(?m)^Pages:\s*(\d+)\s*$", info)
    if match is None or int(match.group(1)) < 1:
        raise IngestionError("corrupt_pdf", "pdfinfo did not report a positive page count")
    return int(match.group(1))


def _read_utf8_text(source: Path | InputSnapshot) -> str:
    try:
        data = source.data if isinstance(source, InputSnapshot) else source.read_bytes()
    except OSError as exc:
        raise IngestionError("extraction", f"cannot read input: {exc.strerror or exc}") from None
    if b"\x00" in data:
        raise IngestionError("encoding_error", "TXT input contains NUL bytes")
    try:
        # A UTF-8 BOM is accepted and removed. No locale fallback or lossy error
        # handling is allowed.
        return data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise IngestionError(
            "encoding_error", f"TXT input is not strict UTF-8 at byte {exc.start}"
        ) from None


def read_input(source: Path | InputSnapshot) -> str:
    suffix = source.suffix if isinstance(source, InputSnapshot) else source.suffix.lower()
    if suffix != ".pdf":
        return _read_utf8_text(source)

    path = source.pdf_path() if isinstance(source, InputSnapshot) else source
    pages = _pdf_page_count(path)
    output = _run_pdf_tool("pdftotext", ["-layout", "-enc", "UTF-8", str(path), "-"])
    text = _decode_tool_output("pdftotext", output)
    visible_characters = len(re.findall(r"[^\s\x0c]", text))
    if visible_characters == 0:
        raise IngestionError("empty_extraction", "pdftotext extracted no visible text")

    # Poppler terminates each extracted page with form feed. A mismatch means
    # stdout was truncated despite a successful process exit. Sparse output is
    # normally an image-only or damaged PDF and must be reviewed/OCRed first.
    extracted_pages = text.count("\x0c")
    if (pages > 1 and extracted_pages < pages) or (
        visible_characters < pages * MIN_PDF_TEXT_CHARACTERS_PER_PAGE
    ):
        raise IngestionError(
            "partial_extraction",
            f"pdftotext output appears incomplete ({visible_characters} visible characters for {pages} pages)",
        )
    return text


def extract_pdf_links(source: Path | InputSnapshot) -> list[tuple[str, str]]:
    """Return links from the same validated PDF snapshot used for extraction."""
    suffix = source.suffix if isinstance(source, InputSnapshot) else source.suffix.lower()
    if suffix != ".pdf":
        return []
    path = source.pdf_path() if isinstance(source, InputSnapshot) else source
    import html as html_mod

    annotation_report = _decode_tool_output(
        "pdfinfo", _run_pdf_tool("pdfinfo", ["-url", "-enc", "UTF-8", str(path)])
    )
    expected_urls = set(re.findall(r"https?://\S+", annotation_report))
    html = _decode_tool_output(
        "pdftohtml",
        _run_pdf_tool(
            "pdftohtml", ["-stdout", "-i", "-noframes", "-enc", "UTF-8", str(path)]
        ),
    )

    links: list[tuple[str, str]] = []
    anchor_pattern = r"<a\b[^>]*\bhref=([\"'])(https?://.*?)\1[^>]*>(.*?)</a>"
    for match in re.finditer(anchor_pattern, html, re.I | re.S):
        url = html_mod.unescape(match.group(2))
        label = html_mod.unescape(re.sub(r"<[^>]+>", "", match.group(3)))
        label = re.sub(r"\s+", " ", label).replace("​", "").strip()
        if label:
            links.append((label, url))
    recovered_urls = {url for _, url in links}
    missing = sorted(expected_urls - recovered_urls)
    if missing:
        raise IngestionError(
            "missing_annotations",
            f"pdftohtml omitted {len(missing)} URL annotation(s); first missing URL: {missing[0]}",
        )
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
    """Block publication when a PDF citation destination is absent from the post."""
    missing_urls = sorted({url for _, url in pdf_links if f'href="{url}"' not in text})
    if missing_urls:
        raise IngestionError(
            "missing_citations",
            f"{len(missing_urls)} PDF citation destination(s) are not embedded; "
            f"first missing URL: {missing_urls[0]}. Add an exact TEXT=URL --link for every missing citation",
        )


def front_matter(title: str, date: str, slug: str) -> str:
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    return f'''+++\ntitle = "{safe_title}"\ndate = {date}\ndraft = false\nslug = "{slug}"\nhideSummary = true\nShowToc = false\n+++\n\n'''


def _validate_staged_post(descriptor: int, expected: bytes) -> None:
    """Verify complete content through the retained staging descriptor."""
    try:
        actual = os.pread(descriptor, len(expected) + 1, 0)
    except OSError as exc:
        raise IngestionError(
            "output_write", f"cannot validate staged output: {exc.strerror or exc}"
        ) from None
    if actual != expected:
        raise IngestionError("output_write", "staged output failed complete-content validation")
    try:
        actual.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise IngestionError(
            "output_write", f"staged output is not UTF-8 at byte {exc.start}"
        ) from None


def _atomic_create_post(output: Path | OutputTarget, post: str) -> None:
    """Install through retained directory descriptors without replacing a name."""
    owns_target = not isinstance(output, OutputTarget)
    if owns_target:
        path = output
        try:
            parent_descriptor = _open_directory(path.parent)
        except OSError as exc:
            raise IngestionError("output_write", f"cannot open output parent: {exc}") from None
        target = OutputTarget(path, parent_descriptor, path.name)
    else:
        target = output

    expected = post.encode("utf-8")
    temporary_name: str | None = None
    descriptor = -1
    installed = False
    operation_error: BaseException | None = None
    try:
        for _ in range(100):
            temporary_name = f".{target.name}.{secrets.token_hex(12)}.tmp"
            try:
                descriptor = os.open(
                    temporary_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=target.parent_descriptor,
                )
                break
            except FileExistsError:
                continue
            except OSError as exc:
                raise IngestionError(
                    "output_write", f"cannot create staged output: {exc.strerror or exc}"
                ) from None
        else:
            raise IngestionError("output_write", "cannot allocate a unique staged output")

        try:
            with os.fdopen(os.dup(descriptor), "w", encoding="utf-8", newline="\n") as destination:
                written = destination.write(post)
                if written != len(post):
                    raise IngestionError("output_write", "could not write the complete staged output")
                destination.flush()
                os.fsync(destination.fileno())
        except IngestionError:
            raise
        except OSError as exc:
            raise IngestionError(
                "output_write", f"cannot flush staged output: {exc.strerror or exc}"
            ) from None

        _validate_staged_post(descriptor, expected)
        try:
            os.link(
                temporary_name,
                target.name,
                src_dir_fd=target.parent_descriptor,
                dst_dir_fd=target.parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise IngestionError(
                "output_exists", f"refusing to overwrite existing post: {target.path}"
            ) from None
        except OSError as exc:
            raise IngestionError(
                "output_write", f"cannot install output atomically: {exc.strerror or exc}"
            ) from None
        installed = True
    except BaseException as exc:
        operation_error = exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                # Closing an already flushed and validated descriptor does not
                # alter the installed bytes. Cleanup below must still run.
                pass

    cleanup_error: OSError | None = None
    rollback_error: OSError | None = None
    if temporary_name is not None:
        try:
            os.unlink(temporary_name, dir_fd=target.parent_descriptor)
        except FileNotFoundError:
            pass
        except OSError as exc:
            cleanup_error = exc

    # A successful link is not reported as a successful transaction until its
    # staging name is gone. If that cleanup fails, remove only the destination
    # hard link created by this invocation so callers never receive failure with
    # a newly publishable post in place.
    if cleanup_error is not None and installed:
        try:
            os.unlink(target.name, dir_fd=target.parent_descriptor)
        except OSError as exc:
            rollback_error = exc

    try:
        if cleanup_error is not None:
            detail = cleanup_error.strerror or str(cleanup_error)
            message = f"cannot remove staged output: {detail}"
            if installed:
                if rollback_error is None:
                    message += "; installed output was rolled back"
                else:
                    rollback_detail = rollback_error.strerror or str(rollback_error)
                    message += f"; cannot roll back installed output: {rollback_detail}"
            raise IngestionError("output_cleanup", message) from operation_error
        if operation_error is not None:
            raise operation_error
    finally:
        if owns_target:
            target.close()


def build_parser() -> argparse.ArgumentParser:
    parser = IngestionArgumentParser(description="Create a Hugo post from a PDF or TXT file.")
    parser.add_argument("input", type=Path, help="PDF or TXT file")
    parser.add_argument("--title", required=True, help="Post title")
    parser.add_argument("--date", help="Post date, e.g. 2026-06-02 or 2026-06-02T09:00:00-07:00")
    parser.add_argument("--slug", help="URL slug; defaults to slugified title")
    parser.add_argument("--link", action="append", default=[], help="Embed citation link: TEXT=URL. Can repeat.")
    parser.add_argument("--keep-references", action="store_true", help="Keep a final References section if present")
    parser.add_argument("--output", type=Path, help="Output .md path; must be below content/posts")
    return parser


def run(argv: Sequence[str] | None = None) -> Path:
    """Validate, convert, and exclusively create one post; return its path."""
    args = build_parser().parse_args(argv)
    input_snapshot = validate_input(args.input)
    output: OutputTarget | None = None
    try:
        title = validate_title(args.title)
        slug = validate_slug(args.slug if args.slug is not None else slugify(title))
        date = validate_date(args.date)
        output = validate_output(args.output or POSTS_DIR / f"{slug}.md")
        for item in args.link:
            if "=" not in item or not all(part.strip() for part in item.split("=", 1)):
                raise IngestionError("invalid_link", "--link must use non-empty TEXT=URL values")

        # Both PDF extraction passes consume one private snapshot. TXT decoding
        # consumes the bytes captured from the retained validated descriptor.
        raw_text = read_input(input_snapshot)
        pdf_links = extract_pdf_links(input_snapshot)
        body = parse_body(raw_text, title, args.keep_references)
        body = fix_extraction_artifacts(body)
        body = embed_footnote_links(body, footnote_url_map(pdf_links, raw_text))
        body = embed_links(body, args.link)
        body = "\n\n".join(p.strip() for p in body.split("\n\n") if p.strip())
        if not body:
            raise IngestionError("empty_extraction", "extraction produced no publishable body text")
        check_all_sources_embedded(body, pdf_links)

        post = front_matter(title, date, slug) + body + "\n"
        _atomic_create_post(output, post)
        return output.path
    finally:
        input_snapshot.close()
        if output is not None:
            output.close()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        output = run(argv)
    except IngestionError as exc:
        print(f"ERROR[{exc.category}]: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {output}")
    print("Review, then run: make validate && make reproducible && make build && make verify-routes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
