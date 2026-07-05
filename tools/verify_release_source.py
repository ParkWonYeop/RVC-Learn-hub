#!/usr/bin/env python3
"""Fail when release-relevant source is hidden by repository ignore rules."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

RELEASE_ROOTS = (
    "apps",
    "docs",
    "infra",
    "installers",
    "packages",
    "supply-chain",
    "tests",
    "tools",
)
RELEASE_FILES = (
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "AGENTS.md",
    "CHECKLIST.md",
    "Makefile",
    "README.md",
    "pyproject.toml",
    "requirements-dev.txt",
)
TRANSIENT_DIRECTORIES = {
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "coverage",
    "htmlcov",
    "node_modules",
    "venv",
}


class SourceClosureError(RuntimeError):
    """Raised when release source provenance cannot be established safely."""


def _candidate_paths(repo_root: Path) -> list[str]:
    candidates: list[str] = []
    for relative in RELEASE_FILES:
        path = repo_root / relative
        metadata = _safe_lstat(path, relative)
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceClosureError(f"release source is not a regular file: {relative}")
        candidates.append(relative)

    for relative_root in RELEASE_ROOTS:
        root = repo_root / relative_root
        metadata = _safe_lstat(root, relative_root)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise SourceClosureError(
                f"release source root is not a real directory: {relative_root}"
            )
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            safe_directories: list[str] = []
            for directory in sorted(directories):
                path = current_path / directory
                child_relative = path.relative_to(repo_root).as_posix()
                child_metadata = _safe_lstat(path, child_relative)
                if stat.S_ISLNK(child_metadata.st_mode):
                    raise SourceClosureError(
                        f"release source contains a directory symlink: {child_relative}"
                    )
                if directory in TRANSIENT_DIRECTORIES or directory.endswith(".egg-info"):
                    continue
                if not stat.S_ISDIR(child_metadata.st_mode):
                    raise SourceClosureError(
                        f"release source contains an unsafe directory entry: {child_relative}"
                    )
                safe_directories.append(directory)
            directories[:] = safe_directories
            for filename in sorted(files):
                path = current_path / filename
                child_relative = path.relative_to(repo_root).as_posix()
                child_metadata = _safe_lstat(path, child_relative)
                if not stat.S_ISREG(child_metadata.st_mode):
                    raise SourceClosureError(
                        f"release source contains a non-regular file: {child_relative}"
                    )
                candidates.append(child_relative)
    return sorted(set(candidates))


def _safe_lstat(path: Path, relative: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise SourceClosureError(f"required release source is unavailable: {relative}") from exc


def verify_release_source(repo_root: Path) -> int:
    resolved = repo_root.resolve(strict=True)
    metadata = resolved.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SourceClosureError("repository root must be a real directory")
    candidates = _candidate_paths(resolved)
    payload = b"\0".join(path.encode("utf-8") for path in candidates) + b"\0"
    result = subprocess.run(
        ["git", "-C", str(resolved), "check-ignore", "--no-index", "--stdin", "-z"],
        input=payload,
        check=False,
        capture_output=True,
    )
    if result.returncode not in {0, 1}:
        raise SourceClosureError("git ignore audit could not be completed")
    if result.returncode == 0:
        ignored = sorted(
            path.decode("utf-8", errors="strict") for path in result.stdout.split(b"\0") if path
        )
        if ignored:
            raise SourceClosureError(f"release source is hidden by ignore rules: {ignored[0]}")
    print(f"Release source ignore closure verified ({len(candidates)} files)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        return verify_release_source(arguments.repo_root)
    except (OSError, SourceClosureError, UnicodeError) as exc:
        print(f"Release source closure failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
