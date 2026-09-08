"""Bounded filesystem access and guarded, atomic per-file bundle writes.

Callers must own the bundle and exclude concurrent directory-tree mutation. File
contents are checked optimistically; a multi-file apply is not a transaction.
"""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from contextlib import contextmanager


@dataclass(frozen=True)
class Limits:
    max_files: int = 10_000
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_line_bytes: int = 64 * 1024
    max_findings: int = 10_000
    max_depth: int = 64

    def __post_init__(self) -> None:
        if any(
            not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in vars(self).values()
        ):
            raise ValueError("resource limits must be positive integers")


DEFAULT_LIMITS = Limits()


class BundleError(ValueError):
    """An operational failure, distinct from a document diagnostic."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_bundle",
        path: str | Path | None = None,
        applied: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = str(path) if path is not None else None
        self.applied = list(applied or [])


def bundle_root(path: str | Path) -> Path:
    try:
        root = Path(path).resolve(strict=True)
        if not root.is_dir():
            raise BundleError("bundle path is not a directory", path=path)
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise BundleError(
                "safe bundle access requires descriptor-relative filesystem support",
                code="unsupported_platform",
                path=path,
            )
        return root
    except (OSError, RuntimeError) as exc:
        raise BundleError(str(exc), path=path) from exc


def _relative(path: Path, root: Path) -> Path:
    absolute = path if path.is_absolute() else root / path
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise BundleError("path escapes the bundle", code="unsafe_path", path=path) from exc
    if not relative.parts or any(p in ("..", ".") for p in relative.parts):
        raise BundleError("invalid bundle file path", code="unsafe_path", path=path)
    return relative


@contextmanager
def _parent(path: Path, root: Path) -> Iterator[tuple[int, str]]:
    relative = _relative(path, root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, relative.name
    finally:
        os.close(fd)


def _read_at(fd: int, name: str, limits: Limits) -> str:
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise BundleError("bundle input is not a regular file", code="unsafe_path", path=name)
        if info.st_size > limits.max_file_bytes:
            raise BundleError("file byte limit exceeded", code="resource_limit", path=name)
        chunks: list[bytes] = []
        size = 0
        while True:
            data = os.read(file_fd, min(65536, limits.max_file_bytes + 1 - size))
            if not data:
                break
            size += len(data)
            if size > limits.max_file_bytes:
                raise BundleError("file byte limit exceeded", code="resource_limit", path=name)
            chunks.append(data)
        raw = b"".join(chunks)
        if any(len(line) > limits.max_line_bytes for line in raw.splitlines()):
            raise BundleError("line byte limit exceeded", code="resource_limit", path=name)
        return raw.decode("utf-8")
    finally:
        os.close(file_fd)


def read_text(path: Path, root: Path, limits: Limits = DEFAULT_LIMITS) -> str:
    try:
        with _parent(Path(path), root) as (fd, name):
            return _read_at(fd, name, limits)
    except BundleError:
        raise
    except (OSError, UnicodeError) as exc:
        raise BundleError(str(exc), code="read_error", path=path) from exc


def markdown_paths(root: Path, limits: Limits = DEFAULT_LIMITS) -> list[Path]:
    """Enumerate deterministically without following any descendant symlink."""
    paths: list[Path] = []
    entries_seen = 0
    total_bytes = 0

    def walk(fd: int, relative: Path, depth: int) -> None:
        nonlocal entries_seen, total_bytes
        if depth > limits.max_depth:
            raise BundleError(
                "directory depth limit exceeded", code="resource_limit", path=relative
            )
        with os.scandir(fd) as entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > limits.max_files:
                    raise BundleError(
                        "bundle entry limit exceeded", code="resource_limit", path=root
                    )
                path = relative / entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise BundleError(
                        "bundle descendants must not be symlinks", code="unsafe_path", path=path
                    )
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    try:
                        walk(child, path, depth + 1)
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(info.st_mode):
                    raise BundleError(
                        "bundle descendants must be regular files or directories",
                        code="unsafe_path",
                        path=path,
                    )
                elif entry.name.endswith(".md"):
                    if info.st_size > limits.max_file_bytes:
                        raise BundleError(
                            "file byte limit exceeded", code="resource_limit", path=path
                        )
                    total_bytes += info.st_size
                    if total_bytes > limits.max_total_bytes:
                        raise BundleError(
                            "bundle byte limit exceeded", code="resource_limit", path=root
                        )
                    paths.append(root / path)

    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            walk(fd, Path(), 0)
        finally:
            os.close(fd)
    except BundleError:
        raise
    except OSError as exc:
        raise BundleError(str(exc), code="read_error", path=root) from exc
    return sorted(paths)


def _check_original(fd: int, name: str, original: str | None, limits: Limits) -> int:
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        if original is not None:
            raise BundleError("input disappeared after planning", code="changed_input", path=name)
        return 0o600
    if not stat.S_ISREG(info.st_mode):
        raise BundleError("output destination is not a regular file", code="unsafe_path", path=name)
    if original is None or _read_at(fd, name, limits) != original:
        raise BundleError("input changed after planning", code="changed_input", path=name)
    return stat.S_IMODE(info.st_mode) & 0o777


def apply_changes(
    root: Path,
    changes: dict[str, str],
    originals: dict[str, str | None],
    limits: Limits = DEFAULT_LIMITS,
) -> list[str]:
    """Stage the entire plan, then atomically replace each unchanged destination.

    On failure BundleError.applied identifies completed replacements. Previously
    replaced files are not silently rolled back over potentially concurrent edits.
    """
    root = bundle_root(root)
    if changes.keys() - originals.keys():
        raise BundleError("every write requires an expected original", code="invalid_plan")
    if len(changes) > limits.max_files:
        raise BundleError("write count limit exceeded", code="resource_limit")
    total = 0
    for rel, text in changes.items():
        _relative(Path(rel), root)
        data = text.encode("utf-8")
        total += len(data)
        if len(data) > limits.max_file_bytes or total > limits.max_total_bytes:
            raise BundleError("output byte limit exceeded", code="resource_limit", path=rel)
        if any(len(line) > limits.max_line_bytes for line in data.splitlines()):
            raise BundleError("output line limit exceeded", code="resource_limit", path=rel)
    staged: list[tuple[int, str, str, str]] = []
    applied: list[str] = []
    failure: BundleError | None = None
    try:
        for rel, text in sorted(changes.items()):
            with _parent(Path(rel), root) as (parent_fd, name):
                mode = _check_original(parent_fd, name, originals[rel], limits)
                fd = os.dup(parent_fd)
                temporary = ".okf-" + secrets.token_hex(16) + ".tmp"
                staged.append((fd, temporary, name, rel))
                file_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=fd,
                )
                try:
                    data = memoryview(text.encode("utf-8"))
                    while data:
                        written = os.write(file_fd, data)
                        data = data[written:]
                    os.fchmod(file_fd, mode)
                    os.fsync(file_fd)
                finally:
                    os.close(file_fd)
        # Recheck the entire plan before the first replacement.
        for fd, _, name, rel in staged:
            _check_original(fd, name, originals[rel], limits)
        for fd, temporary, name, rel in staged:
            _check_original(fd, name, originals[rel], limits)
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            applied.append(rel)
            os.fsync(fd)
        return applied
    except (OSError, BundleError) as exc:
        code = exc.code if isinstance(exc, BundleError) else "write_error"
        path = exc.path if isinstance(exc, BundleError) else None
        failure = BundleError(str(exc), code=code, path=path, applied=applied)
        raise failure from exc
    finally:
        cleanup_errors: list[str] = []
        for fd, temporary, _, _ in staged:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_errors.append(f"{temporary}: {exc}")
            finally:
                os.close(fd)
        if cleanup_errors:
            message = "staging cleanup incomplete: " + "; ".join(cleanup_errors)
            if failure is not None:
                failure.args = (f"{failure}; {message}",)
            else:
                raise BundleError(message, code="cleanup_error", applied=applied)
