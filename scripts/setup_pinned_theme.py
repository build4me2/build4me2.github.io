#!/usr/bin/env python3
"""Materialize the pinned PaperMod checkout from the committed offline snapshot."""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PINNED_COMMIT = "154d006e0182dfc7da38008323976b02e6bfab4a"
PINNED_TREE = "56f58cecea021dfd6226e75dcb10c652957be310"
VENDOR = ROOT / "scripts" / "vendor"
ARCHIVE = VENDOR / f"PaperMod-{PINNED_COMMIT}.tar.gz"
COMMIT_OBJECT = VENDOR / f"PaperMod-{PINNED_COMMIT}.commit"
ARCHIVE_SHA256 = "d8329d2650b130bdd0c912136a2d385ed97add08d21d907054be9ca1da1e1882"
COMMIT_OBJECT_SHA256 = "38906c2337985dbaaa6a6c45ca6ed73ca3d605d67ec5ffcc469c367615c60d04"


class SetupError(RuntimeError):
    """An actionable, stable setup failure."""


def run_git(arguments: list[str], *, cwd: Path, input_bytes: bytes | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments], cwd=cwd, input=input_bytes, capture_output=True,
            text=input_bytes is None, timeout=30,
        )
    except FileNotFoundError as exc:
        raise SetupError("Git is required to materialize the pinned PaperMod checkout") from exc
    except subprocess.TimeoutExpired as exc:
        raise SetupError("Git did not complete pinned PaperMod setup within 30 seconds") from exc
    if result.returncode:
        diagnostic = result.stderr if isinstance(result.stderr, str) else result.stderr.decode("utf-8", "replace")
        diagnostic = diagnostic.strip().splitlines()
        detail = diagnostic[-1] if diagnostic else f"exit status {result.returncode}"
        raise SetupError(f"Git could not materialize pinned PaperMod: {detail}")
    output = result.stdout
    return (output if isinstance(output, str) else output.decode("ascii")).strip()


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def verify_payload(path: Path, expected: str, label: str) -> None:
    if not path.is_file() or sha256(path) != expected:
        raise SetupError(f"Committed PaperMod {label} is missing or has an invalid SHA-256 digest")


def extract_snapshot(destination: Path) -> None:
    """Extract only regular files/directories, independent of tar implementation defaults."""
    with tarfile.open(ARCHIVE, mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise SetupError("Committed PaperMod snapshot contains an unsafe path")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise SetupError("Committed PaperMod snapshot contains an unreadable file")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o644)
            else:
                raise SetupError("Committed PaperMod snapshot contains an unsupported entry type")


def checked_out_commit(destination: Path) -> str | None:
    if not destination.is_dir() or not any(destination.iterdir()):
        return None
    result = subprocess.run(
        ["git", "-C", str(destination), "rev-parse", "--verify", "HEAD"],
        text=True, capture_output=True, timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def materialize(destination: Path) -> bool:
    """Create the exact checkout; return False when it was already ready."""
    actual = checked_out_commit(destination)
    if actual == PINNED_COMMIT:
        return False
    if destination.exists() and any(destination.iterdir()):
        raise SetupError(
            f"PaperMod destination is not empty and is at {actual!r}; refusing to replace local files"
        )

    verify_payload(ARCHIVE, ARCHIVE_SHA256, "snapshot")
    verify_payload(COMMIT_OBJECT, COMMIT_OBJECT_SHA256, "commit object")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.rmdir()

    with tempfile.TemporaryDirectory(prefix="papermod-setup-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "PaperMod"
        staging.mkdir()
        extract_snapshot(staging)
        run_git(["init", "--quiet"], cwd=staging)
        run_git(["config", "core.autocrlf", "false"], cwd=staging)
        run_git(["config", "core.filemode", "true"], cwd=staging)
        run_git(["add", "--all"], cwd=staging)
        tree = run_git(["write-tree"], cwd=staging)
        if tree != PINNED_TREE:
            raise SetupError(f"PaperMod snapshot produced tree {tree}; expected {PINNED_TREE}")
        commit = run_git(
            ["hash-object", "-t", "commit", "-w", "--stdin"],
            cwd=staging, input_bytes=COMMIT_OBJECT.read_bytes(),
        )
        if commit != PINNED_COMMIT:
            raise SetupError(f"PaperMod commit payload produced {commit}; expected {PINNED_COMMIT}")
        run_git(["update-ref", "refs/heads/pinned", PINNED_COMMIT], cwd=staging)
        run_git(["symbolic-ref", "HEAD", "refs/heads/pinned"], cwd=staging)
        os.replace(staging, destination)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up the pinned PaperMod checkout without network access."
    )
    parser.add_argument("--destination", type=Path, default=ROOT / "themes" / "PaperMod")
    args = parser.parse_args()
    try:
        changed = materialize(args.destination.resolve())
    except (OSError, tarfile.TarError, SetupError) as exc:
        print(f"Pinned PaperMod setup FAILED: {exc}", file=sys.stderr)
        return 1
    state = "materialized" if changed else "already ready"
    print(f"Pinned PaperMod {PINNED_COMMIT} {state} (offline snapshot verified).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
