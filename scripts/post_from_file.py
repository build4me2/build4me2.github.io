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
import ctypes
import errno
import ipaddress
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, NamedTuple, Sequence
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit

# Public review APIs live in a small independent module so override files can be
# validated without invoking ingestion. Re-export them here alongside the
# citation extraction/matching API for existing converter callers.
try:
    from scripts.citation_overrides import (
        CitationOverride,
        OverrideApplicationResult,
        OverrideUse,
        OverrideValidationError,
        apply_citation_overrides,
        document_identity,
        load_citation_overrides,
        validate_override_document,
    )
except ModuleNotFoundError:  # Direct execution from the scripts/ directory.
    from citation_overrides import (  # type: ignore[no-redef]
        CitationOverride,
        OverrideApplicationResult,
        OverrideUse,
        OverrideValidationError,
        apply_citation_overrides,
        document_identity,
        load_citation_overrides,
        validate_override_document,
    )

try:
    from scripts.citation_audit import build_audit_report, render_json, render_text
except ModuleNotFoundError:  # Direct execution from the scripts/ directory.
    from citation_audit import build_audit_report, render_json, render_text  # type: ignore[no-redef]

BLOG_ROOT = Path(__file__).resolve().parents[1]
POSTS_DIR = BLOG_ROOT / "content" / "posts"
SUPPORTED_INPUT_SUFFIXES = frozenset({".pdf", ".txt"})
MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_TITLE_LENGTH = 300
MAX_SLUG_LENGTH = 100
PDF_TOOL_TIMEOUT_SECONDS = 30
PDF_TOOL_CLEANUP_TIMEOUT_SECONDS = 1
MAX_TOOL_DIAGNOSTIC_CHARS = 1000
MIN_PDF_TEXT_CHARACTERS_PER_PAGE = 20
SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DEFAULT_OVERRIDE_FILE = BLOG_ROOT / "citation-overrides.json"
_LAST_AUDIT_REPORT: dict[str, object] | None = None


class IngestionError(Exception):
    """An expected, categorized command failure safe to show to a user."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.audit_report: dict[str, object] | None = None


class IngestionArgumentParser(argparse.ArgumentParser):
    """Turn argparse failures into the CLI's stable diagnostic format."""

    def error(self, message: str) -> None:
        raise IngestionError("usage", message)

# DOI's registrant component is 4-9 digits.  The suffix character set follows
# the Crossref-recommended permissive matcher; terminal prose punctuation is
# removed separately so evidence is never discarded from the record.
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
URL_START_PATTERN = re.compile(r"https?://", re.IGNORECASE)
URL_TOKEN_PATTERN = re.compile(r"[^\s<>\"'`]+")


class SourceLocation(NamedTuple):
    """Stable, human-readable location of candidate evidence."""

    source: str
    page: int | None
    line: int | None
    section: str
    annotation: int | None = None


class ExplicitLink(NamedTuple):
    """One validated citation label and its canonical safe destination."""

    label: str
    destination: str


class CitationCandidate(NamedTuple):
    """One immutable citation destination together with its original evidence."""

    raw_evidence: str
    normalized_destination: str
    extraction_method: str
    source_location: SourceLocation
    provenance: str

    # Convenient names for callers which deal specifically in URLs.
    @property
    def raw(self) -> str:
        return self.raw_evidence

    @property
    def normalized_url(self) -> str:
        return self.normalized_destination

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready representation without losing null locations."""
        return {
            "raw_evidence": self.raw_evidence,
            "normalized_destination": self.normalized_destination,
            "extraction_method": self.extraction_method,
            "source_location": self.source_location._asdict(),
            "provenance": self.provenance,
        }


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
        self._pdf_descriptor = -1

    def __eq__(self, other: object) -> bool:
        # Preserve the small public helper's historical convenience in callers.
        return self.path == other

    def pdf_path(self) -> Path:
        """Return an inherited-fd path for one immutable, unlinked PDF copy."""
        if self._pdf_descriptor < 0:
            descriptor = -1
            try:
                descriptor, name = tempfile.mkstemp(prefix="hugo-ingestion-", suffix=".pdf")
            except OSError as exc:
                raise IngestionError(
                    "extraction", f"cannot create validated PDF snapshot: {exc.strerror or exc}"
                ) from None

            # Unlink immediately, before copying any validated bytes. Thus the
            # snapshot is never a mutable named input, even during construction.
            try:
                os.unlink(name)
            except OSError as exc:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise IngestionError(
                    "snapshot_cleanup",
                    f"cannot unlink validated PDF snapshot: {exc.strerror or exc}",
                ) from None

            try:
                with os.fdopen(os.dup(descriptor), "wb") as destination:
                    destination.write(self.data)
                    destination.flush()
                    os.fsync(destination.fileno())
            except OSError as exc:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise IngestionError(
                    "extraction", f"cannot create validated PDF snapshot: {exc.strerror or exc}"
                ) from None
            self._pdf_descriptor = descriptor
        return Path(f"/proc/self/fd/{self._pdf_descriptor}")

    def pdf_pass_fds(self) -> tuple[int, ...]:
        self.pdf_path()
        return (self._pdf_descriptor,)

    def close(self) -> IngestionError | None:
        """Close retained descriptors, returning (never throwing) a categorized failure."""
        errors: list[str] = []
        for attribute, label in (
            ("_pdf_descriptor", "PDF snapshot"),
            ("descriptor", "validated input"),
        ):
            descriptor = getattr(self, attribute)
            if descriptor < 0:
                continue
            # Mark it closed first so destructor retries cannot close an fd that
            # the process may since have reused after an ambiguous close error.
            setattr(self, attribute, -1)
            try:
                os.close(descriptor)
            except OSError as exc:
                errors.append(f"cannot close {label}: {exc.strerror or exc}")
        if errors:
            return IngestionError("snapshot_cleanup", "; ".join(errors))
        return None

    def __del__(self) -> None:
        # Destructors cannot report errors safely. Normal ingestion explicitly
        # closes the snapshot before publication and handles this return value.
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
    # Python strings can contain isolated UTF-16 surrogate code points even
    # though they cannot be represented by strict UTF-8. Reject them as invalid
    # metadata here, before output binding, extraction, or anonymous staging.
    try:
        title.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise IngestionError(
            "invalid_title",
            f"title is not UTF-8-representable at character {exc.start}",
        ) from None
    # Reject the complete C0, DEL, and C1 control ranges. In particular, C1
    # characters can be invisible in terminal arguments and must not be allowed
    # to reach generated TOML metadata even when a parser happens to accept them.
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in title):
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
            # A directory-descriptor close cannot change the transaction. Mark
            # it closed first and never turn a completed installation into a
            # reported failure because close(2) returned an ambiguous error.
            descriptor = self.parent_descriptor
            self.parent_descriptor = -1
            try:
                os.close(descriptor)
            except OSError:
                pass

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


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    """Close captured pipes without allowing cleanup errors to hide a timeout."""
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _terminate_pdf_tool_group(
    process: subprocess.Popen[bytes], timeout_stderr: bytes | None
) -> bytes:
    """Terminate an isolated tool process group without unbounded waiting.

    Poppler itself is normally a single process, but wrappers and malformed-tool
    fixtures can spawn descendants which retain the captured pipe descriptors.
    Killing only the direct child would then leave ``communicate`` blocked.  All
    descendants inherit the new process group, so signal that group, allow one
    bounded graceful interval, then force the complete group down.
    """
    stderr = timeout_stderr or b""
    for group_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, group_signal)
        except ProcessLookupError:
            pass
        except OSError:
            # The group should exist because Popen created a new session. If a
            # platform/runtime race prevents group signalling, still target the
            # direct process; every subsequent wait remains bounded.
            try:
                process.send_signal(group_signal)
            except OSError:
                pass
        try:
            _, cleanup_stderr = process.communicate(
                timeout=PDF_TOOL_CLEANUP_TIMEOUT_SECONDS
            )
            stderr = cleanup_stderr or stderr
            if group_signal == signal.SIGKILL:
                return stderr
            # Reaping the direct process and reaching EOF does not prove that a
            # descendant which closed its inherited pipes also exited. Continue
            # to SIGKILL the group after the graceful interval in all cases.
        except subprocess.TimeoutExpired as exc:
            if exc.stderr:
                stderr = exc.stderr

    # A hostile descendant could deliberately leave the process group and keep
    # an inherited pipe open. Never perform an unbounded final communicate in
    # that case. Closing our pipe ends and making one bounded direct-child reap
    # attempt keeps timeout handling finite.
    _close_process_pipes(process)
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=PDF_TOOL_CLEANUP_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return stderr


def _run_pdf_tool(
    tool: str, arguments: list[str], *, pass_fds: tuple[int, ...] = ()
) -> bytes:
    """Run one required Poppler tool in an isolated, timeout-safe process group."""
    executable = shutil.which(tool)
    if executable is None:
        raise IngestionError("missing_tool", f"required PDF tool '{tool}' was not found; install poppler-utils")
    try:
        process = subprocess.Popen(
            [executable, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
            start_new_session=True,
        )
    except OSError as exc:
        raise IngestionError("tool_failed", f"could not execute {tool}: {exc.strerror or exc}") from None
    try:
        stdout, stderr = process.communicate(timeout=PDF_TOOL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        stderr = _terminate_pdf_tool_group(process, exc.stderr)
        detail = _diagnostic(stderr)
        raise IngestionError(
            "tool_timeout",
            f"{tool} exceeded the {PDF_TOOL_TIMEOUT_SECONDS}-second timeout ({detail})",
        ) from None
    if process.returncode != 0:
        raise IngestionError(
            "tool_failed",
            f"{tool} exited with status {process.returncode}: {_diagnostic(stderr)}",
        )
    return stdout


def _decode_tool_output(tool: str, output: bytes) -> str:
    try:
        return output.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise IngestionError(
            "encoding_error", f"{tool} produced non-UTF-8 output at byte {exc.start}"
        ) from None


def _pdf_page_count(path: Path, *, pass_fds: tuple[int, ...] = ()) -> int:
    try:
        raw_info = _run_pdf_tool(
            "pdfinfo", ["-enc", "UTF-8", str(path)], pass_fds=pass_fds
        )
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
    pass_fds = source.pdf_pass_fds() if isinstance(source, InputSnapshot) else ()
    pages = _pdf_page_count(path, pass_fds=pass_fds)
    output = _run_pdf_tool(
        "pdftotext", ["-layout", "-enc", "UTF-8", str(path), "-"], pass_fds=pass_fds
    )
    text = _decode_tool_output("pdftotext", output)
    visible_characters = len(re.findall(r"[^\s\x0c]", text))
    if visible_characters == 0:
        raise IngestionError("empty_extraction", "pdftotext extracted no visible text")

    # In normal stdout mode Poppler terminates every page, including the last,
    # with a form feed. Validate that framing before inspecting page contents:
    # an aggregate character count cannot prove that every reported page was
    # emitted, and text outside the final delimiter is not attributable to a
    # page. A mismatch can indicate successful-but-truncated tool output.
    page_chunks = text.split("\x0c")
    extracted_pages = len(page_chunks) - 1
    if extracted_pages != pages or page_chunks[-1].strip():
        raise IngestionError(
            "partial_extraction",
            f"pdftotext page framing does not match pdfinfo "
            f"({extracted_pages} page separator(s) for {pages} reported pages)",
        )

    # Check each page independently. A long page must never be allowed to pad
    # an empty or implausibly sparse page past an aggregate-length threshold.
    for page_number, page_text in enumerate(page_chunks[:-1], 1):
        page_visible_characters = len(re.findall(r"\S", page_text))
        if page_visible_characters < MIN_PDF_TEXT_CHARACTERS_PER_PAGE:
            raise IngestionError(
                "partial_extraction",
                f"pdftotext page {page_number} appears incomplete "
                f"({page_visible_characters} visible characters; minimum is "
                f"{MIN_PDF_TEXT_CHARACTERS_PER_PAGE})",
            )
    return text


def _trim_destination(value: str) -> str:
    """Remove punctuation which clearly belongs to surrounding prose."""
    value = value.strip().replace("\u200b", "")
    value = value.rstrip(".,;:!?")
    # Keep balanced parentheses, which are valid and common in DOI suffixes,
    # while removing a closing delimiter introduced by prose.
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    while value.endswith("]") or value.endswith("}"):
        value = value[:-1]
    return value


def _normalize_http_url(raw: str) -> str | None:
    """Conservatively normalize an explicit HTTP(S) token."""
    import html as html_mod

    value = _trim_destination(html_mod.unescape(raw).replace("\n", "").replace("\r", "").replace("\x0c", ""))
    try:
        parsed = urlsplit(value)
        port = parsed.port  # Force validation of malformed ports.
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if "." not in host and host != "localhost":
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.username is not None:
        credentials = parsed.username
        if parsed.password is not None:
            credentials += f":{parsed.password}"
        netloc = f"{credentials}@{netloc}"
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, parsed.fragment))


def _normalize_doi(raw: str) -> tuple[str, str] | None:
    compact = re.sub(r"[\r\n\x0c]+", "", raw).strip()
    compact = re.sub(r"(?i)^doi\s*:\s*", "", compact)
    if compact.lower().startswith("https://doi.org/") or compact.lower().startswith("http://doi.org/"):
        compact = compact.split("doi.org/", 1)[1]
    compact = _trim_destination(compact)
    match = DOI_PATTERN.fullmatch(compact)
    if match is None or compact.endswith("/"):
        return None
    doi = match.group(0).lower()
    return doi, f"https://doi.org/{doi}"


def _line_records(text: str) -> list[tuple[int, int, str, str]]:
    """Return (page, line-on-page, section, text) records in source order."""
    records: list[tuple[int, int, str, str]] = []
    section = "body"
    for page_number, page in enumerate(text.split("\x0c"), 1):
        for line_number, line in enumerate(page.splitlines(), 1):
            if re.match(r"^\s*(references|bibliography|works cited)\s*$", line, re.I):
                section = "references"
            records.append((page_number, line_number, section, line))
    return records


def _wrapped_token(records: list[tuple[int, int, str, str]], index: int, start: int) -> str:
    """Recover an URL/DOI token wrapped after an explicit continuation mark.

    Joining is deliberately narrow: an ordinary complete URL followed by prose
    is never concatenated.  A continuation is accepted only after URL syntax
    which requires or strongly signals more input (slash, query separator,
    fragment, hyphen, equals), including across a PDF form-feed page boundary.
    """
    token_match = URL_TOKEN_PATTERN.match(records[index][3], start)
    if token_match is None:
        return ""
    raw = token_match.group(0)
    current = index
    while token_match.end() == len(records[current][3]) and current + 1 < len(records):
        next_line = records[current + 1][3]
        leading = len(next_line) - len(next_line.lstrip())
        continuation = URL_TOKEN_PATTERN.match(next_line, leading)
        if continuation is None:
            break
        page_break = records[current + 1][0] != records[current][0]
        if not (
            raw.endswith(("/", "-", "_", "?", "&", "=", "#"))
            or _normalize_http_url(raw) is None
            or page_break
        ):
            break
        raw += "\x0c" if page_break else "\n"
        raw += continuation.group(0)
        current += 1
        token_match = continuation
    return raw


def extract_citation_candidates(
    raw_text: str,
    pdf_links: Sequence[tuple[str, str] | tuple[str, str, object] | Mapping[str, object]] = (),
    *,
    source: str = "input",
) -> list[CitationCandidate]:
    """Extract explicit citation destinations in deterministic source order.

    Sources include PDF hyperlink annotations, visible HTTP(S) URLs (including
    conservative line/page wraps), DOI resolver URLs, ``doi:`` forms, and bare
    DOI identifiers.  The function does not infer links from titles, authors,
    or other prose and performs no network access.
    """
    found: list[CitationCandidate] = []

    for annotation_index, annotation in enumerate(pdf_links, 1):
        if isinstance(annotation, Mapping):
            raw_url = str(annotation.get("url", ""))
            page_value = annotation.get("page")
            page = page_value if isinstance(page_value, int) else None
            raw_label = str(annotation.get("label", ""))
        else:
            raw_label, raw_url = str(annotation[0]), str(annotation[1])
            page = annotation[2] if len(annotation) > 2 and isinstance(annotation[2], int) else None
        normalized = _normalize_http_url(raw_url)
        if normalized is None:
            continue
        found.append(CitationCandidate(
            raw_evidence=raw_url,
            normalized_destination=normalized,
            extraction_method="pdf_annotation",
            source_location=SourceLocation(source, page, None, "annotation", annotation_index),
            provenance=f"PDF hyperlink annotation {annotation_index}; label={raw_label!r}",
        ))

    records = _line_records(raw_text)
    for record_index, (page, line, section, value) in enumerate(records):
        occupied: list[tuple[int, int]] = []
        for start_match in URL_START_PATTERN.finditer(value):
            raw = _wrapped_token(records, record_index, start_match.start())
            normalized = _normalize_http_url(raw)
            if normalized is None:
                continue
            doi = _normalize_doi(normalized)
            method = "doi_url" if doi is not None else ("visible_url_wrapped" if re.search(r"[\n\x0c]", raw) else "visible_url")
            destination = doi[1] if doi is not None else normalized
            found.append(CitationCandidate(
                raw, destination, method, SourceLocation(source, page, line, section),
                "visible body text" if section == "body" else "visible reference-section text",
            ))
            occupied.append((start_match.start(), start_match.start() + len(raw.splitlines()[0])))

        # DOI identifiers are evidence in their own right.  Exclude those
        # already represented by a visible resolver URL on this line.
        doi_text = value
        for match in re.finditer(r"(?i)(doi\s*:\s*)?(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", doi_text):
            if any(left <= match.start() < right for left, right in occupied):
                continue
            raw = match.group(0)
            normalized_doi = _normalize_doi(raw)
            if normalized_doi is None:
                continue
            found.append(CitationCandidate(
                raw, normalized_doi[1], "doi_prefixed" if match.group(1) else "bare_doi",
                SourceLocation(source, page, line, section),
                "visible body text" if section == "body" else "visible reference-section text",
            ))

        # Explicit DOI forms split immediately after the slash are unambiguous.
        split_match = re.search(r"(?i)(doi\s*:\s*)?(10\.\d{4,9}/)\s*$", value)
        if split_match and record_index + 1 < len(records):
            continuation = re.match(r"\s*([-._;()/:A-Z0-9]+)", records[record_index + 1][3], re.I)
            if continuation:
                separator = "\x0c" if records[record_index + 1][0] != page else "\n"
                raw = split_match.group(0).rstrip() + separator + continuation.group(1)
                normalized_doi = _normalize_doi(raw)
                if normalized_doi is not None:
                    found.append(CitationCandidate(
                        raw, normalized_doi[1], "doi_prefixed_wrapped" if split_match.group(1) else "bare_doi_wrapped",
                        SourceLocation(source, page, line, section),
                        "visible body text" if section == "body" else "visible reference-section text",
                    ))

    # Do not deduplicate here. Repeated evidence is significant to deterministic
    # matching and must receive an explicit duplicate/ambiguous disposition.
    return found


class SentenceRecord(NamedTuple):
    """A body sentence with a stable source-order identity."""

    record_id: str
    text: str
    source_location: SourceLocation


class InTextCitationRecord(NamedTuple):
    """One marker, author-year form, or inline destination in a sentence."""

    record_id: str
    sentence_id: str
    raw_evidence: str
    identity: str
    form: str
    source_location: SourceLocation


class ReferenceEntryRecord(NamedTuple):
    """One reference-list entry and its deterministic citation identity."""

    record_id: str
    text: str
    identity: str | None
    source_locations: tuple[SourceLocation, ...]


class CitationDisposition(NamedTuple):
    """Final non-guessing disposition for one parsed or recovered record."""

    subject_type: str
    subject_id: str
    status: str
    matched_id: str | None
    reason: str


class CitationMatchResult(NamedTuple):
    sentences: tuple[SentenceRecord, ...]
    citations: tuple[InTextCitationRecord, ...]
    references: tuple[ReferenceEntryRecord, ...]
    candidates: tuple[CitationCandidate, ...]
    dispositions: tuple[CitationDisposition, ...]

    @property
    def blocking(self) -> bool:
        return any(item.status != "matched" for item in self.dispositions)


def _author_year_identity(value: str) -> str | None:
    """Return the deliberately narrow ``surname:year`` identity.

    A suffix (2020a) is part of the year. Only the first author surname is used;
    consequently two entries sharing it are reported ambiguous rather than
    disambiguated from titles or guessed author lists.
    """
    year = re.search(r"\b((?:18|19|20)\d{2}[a-z]?)\b", value, re.I)
    if year is None:
        return None
    prefix = value[:year.start()]
    prefix = re.sub(r"^[\s\[(]*(?:see|e\.g\.|cf\.)\s+", "", prefix, flags=re.I)
    surname = re.search(r"[A-Z][A-Za-z'’-]+", prefix)
    if surname is None:
        return None
    return f"author-year:{surname.group(0).casefold()}:{year.group(1).casefold()}"


def _reference_entries(raw_text: str, source: str) -> list[ReferenceEntryRecord]:
    records = _line_records(raw_text)
    entries: list[tuple[list[str], list[SourceLocation], str | None]] = []
    current_lines: list[str] = []
    current_locations: list[SourceLocation] = []
    current_identity: str | None = None

    def finish() -> None:
        nonlocal current_lines, current_locations, current_identity
        if current_lines:
            entries.append((current_lines, current_locations, current_identity))
        current_lines, current_locations, current_identity = [], [], None

    for page, line, section, value in records:
        if section != "references" or re.match(
            r"^\s*(references|bibliography|works cited)\s*$", value, re.I
        ):
            continue
        if not value.strip():
            continue
        numeric = re.match(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)])\s+", value)
        author_identity = _author_year_identity(value)
        # An entry starts only at a numeric label or a plausible author/year
        # line. Indented/nonmatching lines continue the prior entry.
        starts_entry = numeric is not None or (
            author_identity is not None and re.match(r"^\s*[A-Z][A-Za-z'’-]+", value) is not None
        )
        if starts_entry:
            finish()
            if numeric is not None:
                current_identity = f"numeric:{numeric.group(1) or numeric.group(2)}"
            else:
                current_identity = author_identity
        if starts_entry or current_lines:
            current_lines.append(value.strip())
            current_locations.append(SourceLocation(source, page, line, "references"))
    finish()
    return [
        ReferenceEntryRecord(
            f"reference-{index:04d}", " ".join(lines), identity, tuple(locations)
        )
        for index, (lines, locations, identity) in enumerate(entries, 1)
    ]


def parse_citation_records(
    raw_text: str, *, source: str = "input"
) -> tuple[list[SentenceRecord], list[InTextCitationRecord], list[ReferenceEntryRecord]]:
    """Parse review records without title, network, or fuzzy matching.

    Sentence and record IDs depend only on source order. Numeric citations are
    bracketed numbers/lists or 1-2 digit markers directly following sentence
    punctuation. Author-year citations require a four-digit year and an
    explicit parenthesized form. URLs in Markdown/HTML links become inline
    citation identities after the same conservative normalization used by the
    candidate extractor.
    """
    sentences: list[SentenceRecord] = []
    citations: list[InTextCitationRecord] = []
    for page, line, section, value in _line_records(raw_text):
        if section != "body" or not value.strip():
            continue
        # Line-local splitting is intentional: Poppler line locations remain
        # auditable and no heuristic paragraph reconstruction changes IDs.
        parts = [
            part for part in re.findall(
                r".*?(?:[.!?](?:\[(?:\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\]|[1-9]\d?)?(?=\s|$)|$)",
                value.strip(),
            ) if part
        ]
        for part in parts:
            sentence = SentenceRecord(
                f"sentence-{len(sentences) + 1:04d}", part.strip(),
                SourceLocation(source, page, line, "body"),
            )
            sentences.append(sentence)
            discovered: list[tuple[int, str, str, str]] = []
            for marker in re.finditer(
                r"\[(\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\]|\((\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\)",
                part,
            ):
                values = marker.group(1) or marker.group(2)
                for number in re.findall(r"\d{1,3}", values):
                    discovered.append((marker.start(), marker.group(0), f"numeric:{int(number)}", "numeric"))
            for marker in re.finditer(r"(?<=[.!?])[1-9]\d?(?=\s|$)", part):
                discovered.append((marker.start(), marker.group(0), f"numeric:{int(marker.group(0))}", "numeric"))

            year_pattern = r"(?:18|19|20)\d{2}[a-z]?"
            author_pattern = r"[A-Z][A-Za-z'’-]+(?:\s+(?:et al\.|and|&)\s+[A-Z][A-Za-z'’-]+)?"
            # Narrative form: Smith (2020). Parenthetical lists are split at
            # semicolons so each explicit author/year pair gets its own record.
            for marker in re.finditer(
                rf"\b({author_pattern})\s*\(({year_pattern})\)", part
            ):
                identity = _author_year_identity(marker.group(0))
                if identity is not None:
                    discovered.append((marker.start(), marker.group(0), identity, "author_year"))
            for parenthetical in re.finditer(r"\(([^()]*)\)", part):
                for marker in re.finditer(
                    rf"(?:^|;)\s*({author_pattern})\s*,?\s*({year_pattern})(?=\s*(?:;|$))",
                    parenthetical.group(1),
                ):
                    evidence = marker.group(0).lstrip("; ")
                    identity = _author_year_identity(evidence)
                    if identity is not None:
                        discovered.append((
                            parenthetical.start() + 1 + marker.start(), evidence,
                            identity, "author_year",
                        ))
            link_pattern = r"\[[^\]]+\]\((https?://[^\s)]+)\)|<a\s+[^>]*href=[\"'](https?://[^\"']+)[\"'][^>]*>"
            for marker in re.finditer(link_pattern, part, re.I):
                raw_url = marker.group(1) or marker.group(2)
                normalized = _normalize_http_url(raw_url)
                if normalized is not None:
                    discovered.append((marker.start(), marker.group(0), f"destination:{normalized}", "inline_link"))
            for _, evidence, identity, form in sorted(discovered, key=lambda item: (item[0], item[2], item[1])):
                citations.append(InTextCitationRecord(
                    f"citation-{len(citations) + 1:04d}", sentence.record_id,
                    evidence, identity, form, sentence.source_location,
                ))
    return sentences, citations, _reference_entries(raw_text, source)


def match_citation_candidates(
    raw_text: str,
    candidates: Sequence[CitationCandidate] | None = None,
    *,
    source: str = "input",
) -> CitationMatchResult:
    """Apply exact deterministic citation matching and never choose a tie.

    Numeric/author-year citations match only an identical reference identity.
    Inline links match only an identical destination on the same source line.
    Reference candidates match only by their source line. Duplicate identities,
    repeated candidate destinations, multiple destinations for one reference,
    zero matches, and multiple valid matches remain explicit blocking states.
    """
    sentences, citations, references = parse_citation_records(raw_text, source=source)
    candidate_list = list(candidates) if candidates is not None else extract_citation_candidates(raw_text, source=source)
    dispositions: list[CitationDisposition] = []

    references_by_identity: dict[str, list[ReferenceEntryRecord]] = {}
    for reference in references:
        if reference.identity is not None:
            references_by_identity.setdefault(reference.identity, []).append(reference)

    candidate_owners: dict[int, list[tuple[str, str]]] = {index: [] for index in range(len(candidate_list))}
    candidate_keys: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidate_list):
        candidate_keys.setdefault(candidate.normalized_destination, []).append(index)

    citation_identity_counts: dict[tuple[str, str], int] = {}
    for citation in citations:
        key = (citation.sentence_id, citation.identity)
        citation_identity_counts[key] = citation_identity_counts.get(key, 0) + 1

    for citation in citations:
        matches: list[tuple[str, str]] = []
        if citation_identity_counts[(citation.sentence_id, citation.identity)] > 1:
            dispositions.append(CitationDisposition(
                "citation", citation.record_id, "duplicate_identity", None,
                "citation identity is repeated in the same sentence",
            ))
            continue
        if citation.form == "inline_link":
            destination = citation.identity.removeprefix("destination:")
            for index in candidate_keys.get(destination, []):
                candidate = candidate_list[index]
                if (candidate.source_location.page, candidate.source_location.line) == (
                    citation.source_location.page, citation.source_location.line
                ):
                    matches.append(("candidate", f"candidate-{index + 1:04d}"))
                    candidate_owners[index].append(("citation", citation.record_id))
        else:
            matches = [("reference", item.record_id) for item in references_by_identity.get(citation.identity, [])]
        status = "matched" if len(matches) == 1 else ("unresolved" if not matches else "ambiguous")
        dispositions.append(CitationDisposition(
            "citation", citation.record_id, status, matches[0][1] if len(matches) == 1 else None,
            "one exact deterministic match" if len(matches) == 1 else
            ("no exact reference/candidate match" if not matches else "multiple exact matches; tie not selected"),
        ))

    for reference in references:
        identity_peers = references_by_identity.get(reference.identity, []) if reference.identity else []
        locations = {(item.page, item.line) for item in reference.source_locations}
        owned = [
            index for index, candidate in enumerate(candidate_list)
            if candidate.source_location.section == "references"
            and (candidate.source_location.page, candidate.source_location.line) in locations
        ]
        for index in owned:
            candidate_owners[index].append(("reference", reference.record_id))
        destinations = {candidate_list[index].normalized_destination for index in owned}
        if reference.identity is None:
            status, reason = "malformed", "reference has no numeric or author-year identity"
        elif len(identity_peers) > 1:
            status, reason = "duplicate_identity", "reference identity is not unique"
        elif not owned:
            status, reason = "unlinked", "reference has no citation destination candidate"
        elif len(owned) > 1 and len(destinations) == 1:
            status, reason = "duplicate_candidate", "reference repeats one destination candidate"
        elif len(destinations) > 1:
            status, reason = "conflicting_destination", "reference has multiple distinct destinations"
        else:
            status, reason = "matched", "one exact destination candidate"
        dispositions.append(CitationDisposition(
            "reference", reference.record_id, status,
            f"candidate-{owned[0] + 1:04d}" if status == "matched" else None, reason,
        ))

    for index, candidate in enumerate(candidate_list):
        owners = candidate_owners[index]
        duplicates = candidate_keys[candidate.normalized_destination]
        if len(duplicates) > 1:
            status, reason = "duplicate_candidate", "normalized destination occurs in multiple candidate records"
        elif not owners:
            status, reason = "orphaned", "candidate has no exact citation or reference owner"
        elif len(owners) > 1:
            status, reason = "ambiguous", "candidate has multiple exact owners; tie not selected"
        else:
            status, reason = "matched", "one exact deterministic owner"
        dispositions.append(CitationDisposition(
            "candidate", f"candidate-{index + 1:04d}", status,
            owners[0][1] if status == "matched" else None, reason,
        ))

    # Sentences are first-class records too. A sentence with no citation is not
    # an error; a cited sentence inherits the worst deterministic citation state.
    citation_dispositions = {item.subject_id: item for item in dispositions if item.subject_type == "citation"}
    for sentence in sentences:
        members = [item for item in citations if item.sentence_id == sentence.record_id]
        failures = [citation_dispositions[item.record_id] for item in members if citation_dispositions[item.record_id].status != "matched"]
        status = "unresolved" if failures else "matched"
        reason = "contains unresolved or ambiguous citation records" if failures else (
            "all citation records matched" if members else "sentence contains no citation"
        )
        dispositions.append(CitationDisposition("sentence", sentence.record_id, status, None, reason))

    return CitationMatchResult(
        tuple(sentences), tuple(citations), tuple(references), tuple(candidate_list), tuple(dispositions)
    )


def extract_pdf_links(source: Path | InputSnapshot) -> list[tuple[str, str]]:
    """Return links from the same validated PDF snapshot used for extraction."""
    suffix = source.suffix if isinstance(source, InputSnapshot) else source.suffix.lower()
    if suffix != ".pdf":
        return []
    path = source.pdf_path() if isinstance(source, InputSnapshot) else source
    pass_fds = source.pdf_pass_fds() if isinstance(source, InputSnapshot) else ()
    import html as html_mod

    annotation_report = _decode_tool_output(
        "pdfinfo",
        _run_pdf_tool(
            "pdfinfo", ["-url", "-enc", "UTF-8", str(path)], pass_fds=pass_fds
        ),
    )
    expected_urls = set(re.findall(r"https?://\S+", annotation_report))
    html = _decode_tool_output(
        "pdftohtml",
        _run_pdf_tool(
            "pdftohtml",
            ["-stdout", "-i", "-noframes", "-enc", "UTF-8", str(path)],
            pass_fds=pass_fds,
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


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _parse_legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse historical inet-style IPv4 syntax without DNS or platform APIs.

    Browsers and URL consumers may interpret one- to four-component decimal,
    octal, or hexadecimal hosts as IPv4 even though ``ipaddress`` correctly
    rejects those non-canonical spellings. Recognizing them here prevents a
    private address from passing validation as if it were a DNS name.
    """
    candidate = host[:-1] if host.endswith(".") else host
    components = candidate.split(".")
    if not 1 <= len(components) <= 4 or any(not component for component in components):
        return None

    numbers: list[int] = []
    for component in components:
        if component.lower().startswith("0x"):
            digits, base = component[2:], 16
            if not digits or re.fullmatch(r"[0-9a-fA-F]+", digits) is None:
                return None
        elif len(component) > 1 and component.startswith("0"):
            digits, base = component[1:], 8
            if re.fullmatch(r"[0-7]*", digits) is None:
                return None
        else:
            digits, base = component, 10
            if re.fullmatch(r"[0-9]+", digits) is None:
                return None
        try:
            numbers.append(int(digits or "0", base))
        except ValueError:  # Includes Python's bounded conversion of huge decimal input.
            return None

    final_bits = 8 * (5 - len(numbers))
    if any(number > 255 for number in numbers[:-1]) or numbers[-1] >= 1 << final_bits:
        return None
    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value |= number << (final_bits + 8 * (len(numbers) - index - 2))
    return ipaddress.IPv4Address(value)


def _normalize_explicit_destination(raw: str) -> str:
    """Validate a user-provided destination before it reaches an HTML attribute."""
    if raw != raw.strip() or not raw:
        raise IngestionError("invalid_link", "--link URL must not be empty or have surrounding whitespace")
    if _contains_control(raw) or any(character.isspace() for character in raw):
        raise IngestionError("invalid_link", "--link URL must not contain whitespace or control characters")
    if any(character in raw for character in "\\\"'<>`"):
        raise IngestionError("invalid_link", "--link URL contains a forbidden delimiter")
    if re.search(r"%(?![0-9A-Fa-f]{2})", raw):
        raise IngestionError("invalid_link", "--link URL contains malformed percent encoding")
    try:
        decoded = unquote_to_bytes(raw)
    except UnicodeEncodeError:
        raise IngestionError("invalid_link", "--link URL must use valid URL encoding") from None
    if any(byte < 32 or 127 <= byte <= 159 for byte in decoded):
        raise IngestionError("invalid_link", "--link URL contains an encoded control character")

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise IngestionError("invalid_link", "--link URL is malformed") from None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise IngestionError("invalid_link", "--link URL must be an absolute HTTP(S) destination")
    if parsed.username is not None or parsed.password is not None:
        raise IngestionError("invalid_link", "--link URL must not contain credentials")
    if "%" in parsed.hostname:
        # URL consumers decode host escapes before deciding whether a host is an
        # IP address; accepting them here would permit %31%32%37.1 to bypass the
        # same private-address check as its plain-text 127.1 spelling.
        raise IngestionError("invalid_link", "--link URL host must not use percent encoding")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise IngestionError("invalid_link", "--link URL host is invalid") from None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    legacy_address = _parse_legacy_ipv4(host) if address is None else None
    if legacy_address is not None:
        if not legacy_address.is_global:
            raise IngestionError("invalid_link", "--link URL must not target a private or local address")
        raise IngestionError("invalid_link", "--link URL must use canonical IPv4 notation")
    if host.casefold() == "localhost" or (address is None and "." not in host):
        raise IngestionError("invalid_link", "--link URL must use a public, fully qualified host")
    if address is not None and not address.is_global:
        raise IngestionError("invalid_link", "--link URL must not target a private or local address")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, parsed.fragment))


def parse_explicit_links(values: Sequence[str]) -> tuple[ExplicitLink, ...]:
    """Strictly parse and normalize repeatable ``TEXT=URL`` arguments."""
    parsed: list[ExplicitLink] = []
    labels: set[str] = set()
    for value in values:
        if _contains_control(value):
            raise IngestionError("invalid_link", "--link must not contain control characters")
        if "=" not in value:
            raise IngestionError("invalid_link", "--link must use non-empty TEXT=URL values")
        raw_label, raw_url = value.split("=", 1)
        label = raw_label.strip()
        if not label or not raw_url:
            raise IngestionError("invalid_link", "--link must use non-empty TEXT=URL values")
        if any(character in label for character in "<>"):
            raise IngestionError("invalid_link", "--link TEXT must not contain HTML delimiters")
        if label in labels:
            raise IngestionError("invalid_link", "--link TEXT labels must be unique")
        labels.add(label)
        parsed.append(ExplicitLink(label, _normalize_explicit_destination(raw_url)))
    return tuple(parsed)


def embed_links(text: str, explicit_links: Sequence[ExplicitLink]) -> str:
    """Embed mappings only when each label identifies one unambiguous text span."""
    import html as html_mod

    spans: list[tuple[int, int, ExplicitLink]] = []
    for link in explicit_links:
        occurrences = [match.span() for match in re.finditer(re.escape(link.label), text)]
        if not occurrences:
            raise IngestionError("invalid_link", f"--link TEXT label is absent from extracted body: {link.label!r}")
        if len(occurrences) != 1:
            raise IngestionError("invalid_link", f"--link TEXT label is ambiguous in extracted body: {link.label!r}")
        spans.append((*occurrences[0], link))
    spans.sort(key=lambda item: item[0])
    if any(right_start < left_end for (_, left_end, _), (right_start, _, _) in zip(spans, spans[1:])):
        raise IngestionError("invalid_link", "--link TEXT labels overlap in extracted body")
    for start, end, link in reversed(spans):
        safe_url = html_mod.escape(link.destination, quote=True)
        text = text[:start] + f'<a href="{safe_url}">{link.label}</a>' + text[end:]

    # Remove numeric citation markers after embedding links.
    # Example PDF text: cited sentence.3
    # If --link wrapped the cited sentence, it becomes <a ...>cited sentence.</a>3;
    # remove that trailing citation number too.
    text = re.sub(r"(</a>)\s*([1-9]|1[0-9])(?=\s|$)", r"\1", text)
    # (?<!\d) protects decimals: "9.7 gigabytes" must not lose its 7.
    text = re.sub(r"(?<!\d)([.!?”])\s*([1-9]|1[0-9])(?=\s|$)", r"\1", text)
    text = re.sub(r"([A-Za-z”\)])([1-9]|1[0-9])(?=\s)", r"\1", text)

    # Convert raw URLs into links without changing visible URL text.
    def autolink(fragment: str) -> str:
        def repl(match: re.Match[str]) -> str:
            url = match.group(0)
            clean = url.rstrip(".;,")
            trail = url[len(clean):]
            safe_url = html_mod.escape(clean, quote=True)
            return f'<a href="{safe_url}">{clean}</a>{trail}'

        return re.sub(r"(?<![\"=])https?://[^\s)]+", repl, fragment)

    # Never scan inside anchors just created above; URL labels must not become
    # nested anchors and existing href attributes must remain opaque.
    parts = re.split(r"(<a\b[^>]*>.*?</a>)", text, flags=re.I | re.S)
    return "".join(part if re.fullmatch(r"<a\b[^>]*>.*?</a>", part, re.I | re.S) else autolink(part) for part in parts)


def check_all_sources_embedded(text: str, pdf_links: list[tuple[str, str]]) -> None:
    """Block publication when a PDF citation destination is absent from the post."""
    import html as html_mod

    missing_urls = sorted({
        url for _, url in pdf_links
        if f'href="{html_mod.escape(url, quote=True)}"' not in text
    })
    if missing_urls:
        raise IngestionError(
            "missing_citations",
            f"{len(missing_urls)} PDF citation destination(s) are not embedded; "
            f"first missing URL: {missing_urls[0]}. Add an exact TEXT=URL --link for every missing citation",
        )


def front_matter(title: str, date: str, slug: str) -> str:
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    return f'''+++\ntitle = "{safe_title}"\ndate = {date}\ndraft = false\nslug = "{slug}"\nhideSummary = true\nShowToc = false\n+++\n\n'''


def validate_generated_front_matter(post: str) -> None:
    """Require one syntactically valid TOML front-matter block.

    This validation is deliberately performed on the final generated string,
    rather than assuming the individual metadata validators and escaping logic
    necessarily compose into valid TOML.
    """
    lines = post.splitlines()
    if not lines or lines[0] != "+++":
        raise IngestionError("invalid_front_matter", "generated post is missing TOML front matter")
    try:
        closing = lines.index("+++", 1)
    except ValueError:
        raise IngestionError(
            "invalid_front_matter", "generated TOML front matter is not terminated"
        ) from None
    document = "\n".join(lines[1:closing]) + "\n"
    try:
        parsed = tomllib.loads(document)
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        # Parser text is deterministic for a fixed Python runtime and does not
        # expose paths or generated temporary names.
        raise IngestionError(
            "invalid_front_matter", f"generated TOML front matter is invalid: {exc}"
        ) from None
    required = {"title", "date", "draft", "slug", "hideSummary", "ShowToc"}
    if set(parsed) != required:
        raise IngestionError(
            "invalid_front_matter", "generated TOML front matter has unexpected metadata fields"
        )


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


def _install_anonymous_file(descriptor: int, target: OutputTarget) -> None:
    """Give an O_TMPFILE descriptor its sole name, without replacement.

    Python's os.link does not expose Linux linkat(AT_EMPTY_PATH), so use the
    libc wrapper. This syscall is the transaction's commit point: before it the
    inode has no directory entry; after it the complete inode has exactly the
    destination entry.
    """
    at_empty_path = 0x1000
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = libc.linkat
    linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    linkat.restype = ctypes.c_int
    if linkat(descriptor, b"", target.parent_descriptor, os.fsencode(target.name), at_empty_path) == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise IngestionError(
            "output_exists", f"refusing to overwrite existing post: {target.path}"
        )
    raise IngestionError(
        "output_write", f"cannot install output atomically: {os.strerror(error)}"
    )


def _atomic_create_post(output: Path | OutputTarget, post: str) -> None:
    """Install a fully validated anonymous inode as the destination's sole name."""
    # This is a defensive boundary in addition to metadata/source validation:
    # callers of this helper may still supply a Python string containing an
    # isolated surrogate. Categorize that failure before opening the output
    # directory or creating an O_TMPFILE staging inode.
    try:
        expected = post.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise IngestionError(
            "encoding_error",
            f"generated output is not UTF-8-representable at character {exc.start}",
        ) from None

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

    descriptor = -1
    try:
        # O_TMPFILE creates an inode with link count zero. Every pre-install
        # fault therefore disappears merely by closing the fd: there is no
        # staging pathname to clean up and no rollback operation that can fail.
        if not hasattr(os, "O_TMPFILE"):
            raise IngestionError(
                "output_write", "anonymous output staging is not supported on this platform"
            )
        flags = os.O_RDWR | os.O_TMPFILE | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(".", flags, 0o600, dir_fd=target.parent_descriptor)
        except OSError as exc:
            raise IngestionError(
                "output_write", f"cannot create anonymous staged output: {exc.strerror or exc}"
            ) from None

        try:
            offset = 0
            while offset < len(expected):
                written = os.write(descriptor, expected[offset:])
                if written <= 0:
                    raise IngestionError("output_write", "could not write the complete staged output")
                offset += written
            os.fsync(descriptor)
        except IngestionError:
            raise
        except OSError as exc:
            raise IngestionError(
                "output_write", f"cannot flush staged output: {exc.strerror or exc}"
            ) from None

        _validate_staged_post(descriptor, expected)

        # Keep this as the final fallible transaction operation. Descriptor
        # closes below are deliberately non-reporting because they cannot alter
        # the installed name or leave a named staging artifact.
        _install_anonymous_file(descriptor, target)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
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
    parser.add_argument(
        "--citation-overrides", type=Path, default=DEFAULT_OVERRIDE_FILE,
        help="strict reviewed override document (defaults to the committed repository file)",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> Path:
    """Audit, validate, and exclusively create one post; return its path."""
    global _LAST_AUDIT_REPORT
    _LAST_AUDIT_REPORT = None
    args = build_parser().parse_args(argv)
    input_snapshot = validate_input(args.input)
    output: OutputTarget | None = None
    try:
        title = validate_title(args.title)
        slug = validate_slug(args.slug if args.slug is not None else slugify(title))
        date = validate_date(args.date)
        output = validate_output(args.output or POSTS_DIR / f"{slug}.md")
        explicit_links = parse_explicit_links(args.link)

        extraction_error: BaseException | None = None
        try:
            # Every Poppler pass inherits the same unlinked descriptor. TXT
            # decoding consumes bytes captured from the validated input fd.
            raw_text = read_input(input_snapshot)
            pdf_links = extract_pdf_links(input_snapshot)
            body = parse_body(raw_text, title, args.keep_references)
            body = fix_extraction_artifacts(body)
            # Explicit mappings are resolved against plain extracted prose before
            # any generated anchor markup exists, preventing attribute matches.
            body = embed_links(body, explicit_links)
            body = embed_footnote_links(body, footnote_url_map(pdf_links, raw_text))
            body = "\n\n".join(p.strip() for p in body.split("\n\n") if p.strip())
            if not body:
                raise IngestionError("empty_extraction", "extraction produced no publishable body text")
            check_all_sources_embedded(body, pdf_links)

            # Audit the immutable extracted source, before constructing or
            # installing a publishable post. Overrides may resolve only the
            # narrowly scoped records accepted by citation_overrides.py.
            source_name = input_snapshot.path.name
            identity = document_identity(
                input_snapshot.data if isinstance(input_snapshot.data, bytes) else raw_text
            )
            candidates = extract_citation_candidates(raw_text, pdf_links, source=source_name)
            matched = match_citation_candidates(raw_text, candidates, source=source_name)
            try:
                overrides = load_citation_overrides(args.citation_overrides)
                applied = apply_citation_overrides({identity: matched}, overrides)
            except OverrideValidationError as exc:
                report = build_audit_report(
                    source=source_name, source_identity=identity, result=matched,
                    raw_text=raw_text,
                    errors=[{"category": "citation_overrides", "message": str(exc)}],
                )
                failure = IngestionError("citation_overrides", str(exc))
                failure.audit_report = report
                _LAST_AUDIT_REPORT = report
                raise failure from None
            reviewed = applied.results[identity]
            report = build_audit_report(
                source=source_name, source_identity=identity, result=reviewed,
                override_uses=applied.uses, raw_text=raw_text,
            )
            _LAST_AUDIT_REPORT = report
            if report["summary"]["outcome"] != "success":  # type: ignore[index]
                failure = IngestionError(
                    "citation_audit",
                    f"citation audit has {report['summary']['blockingCount']} blocking finding(s)",  # type: ignore[index]
                )
                failure.audit_report = report
                raise failure
            post = front_matter(title, date, slug) + body + "\n"
            # Parse the complete generated metadata before the transaction can
            # create even an anonymous staging inode.
            validate_generated_front_matter(post)
        except BaseException as exc:
            extraction_error = exc

        snapshot_cleanup_error = input_snapshot.close()
        if extraction_error is not None:
            # Cleanup must never replace the stable primary ingestion failure.
            # If extraction failed before matching, still attach a canonical
            # audit report using the immutable input snapshot identity.
            if isinstance(extraction_error, IngestionError) and extraction_error.audit_report is None:
                identity_data = input_snapshot.data
                identity = document_identity(
                    identity_data if isinstance(identity_data, bytes) else str(identity_data)
                )
                report = build_audit_report(
                    source=str(input_snapshot.path.name), source_identity=identity,
                    errors=[{"category": extraction_error.category, "message": str(extraction_error)}],
                )
                extraction_error.audit_report = report
                _LAST_AUDIT_REPORT = report
            raise extraction_error
        if snapshot_cleanup_error is not None:
            # Snapshot lifecycle completes before atomic publication, so this
            # explicit failure cannot leave a newly publishable destination.
            raise snapshot_cleanup_error

        _atomic_create_post(output, post)
        return output.path
    finally:
        # Idempotent fallback for failures during metadata/output validation.
        input_snapshot.close()
        if output is not None:
            output.close()


def main(argv: Sequence[str] | None = None) -> int:
    global _LAST_AUDIT_REPORT
    try:
        output = run(argv)
    except IngestionError as exc:
        report = exc.audit_report or _LAST_AUDIT_REPORT
        if report is None:
            # Even failures before extraction emit the same report schema. No
            # path resolution or file read is attempted merely for reporting.
            arguments = list(argv) if argv is not None else sys.argv[1:]
            source = Path(arguments[0]).name if arguments and not arguments[0].startswith("-") else "unavailable"
            report = build_audit_report(
                source=source, source_identity="unavailable", errors=[{
                    "category": exc.category, "message": str(exc),
                }],
            )
        print(render_text(report), end="", file=sys.stderr)
        print(render_json(report), end="")
        return 2
    assert _LAST_AUDIT_REPORT is not None
    print(render_text(_LAST_AUDIT_REPORT), end="", file=sys.stderr)
    print(render_json(_LAST_AUDIT_REPORT), end="")
    print(f"Wrote {output}", file=sys.stderr)
    print("Review, then run: make validate && make reproducible && make build && make verify-routes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
