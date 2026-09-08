"""Generate OKF reserved `index.md` files (progressive disclosure).

Per §8, an `index.md` MAY appear in any directory, contains **no frontmatter**, and its body
is sections of `* [Title](url) - description`. It is a reserved filename and MUST NOT be used
for a concept document. Producers MAY generate it; consumers MAY synthesize one when it is
absent.

The one exception is the bundle-root `index.md`, which MAY carry an `okf_version` key (§8,
§12) — the only place frontmatter is permitted in an index. That declaration is preserved
across regeneration.

Note that a file named `_index.md` (leading underscore) has no meaning in OKF. It is an
ordinary concept document. Vaults that use `_index.md` as a folder note can keep them: the
two coexist without conflict.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

import yaml

from .parser import BundleSnapshot, Document, load_bundle, split_frontmatter
from .safety import DEFAULT_LIMITS, BundleError, Limits, apply_changes

_MARKDOWN_PUNCTUATION = re.compile(r"([\\`*{}\[\]<>()#+.!_|~\-])")


def _escape_text(value: str) -> str:
    return _MARKDOWN_PUNCTUATION.sub(r"\\\1", " ".join(value.splitlines()))


def _preserved_version(document: Document | None) -> str:
    """Serialize only the permitted bundle-root version; never interpolate YAML."""
    if document is None or document.frontmatter is None:
        return ""
    fm = document.frontmatter
    if fm.keys() - {"okf_version"}:
        raise BundleError(
            "root index frontmatter permits only `okf_version`",
            code="invalid_metadata",
            path=document.rel,
        )
    if "okf_version" not in fm:
        return ""
    version = fm["okf_version"]
    if not isinstance(version, str) or not version.strip():
        raise BundleError(
            "`okf_version` must be a non-empty string",
            code="invalid_metadata",
            path=document.rel,
        )
    return (
        "---\n"
        + yaml.safe_dump(
            {"okf_version": version},
            sort_keys=False,
            allow_unicode=True,
        )
        + "---\n\n"
    )


def build_indexes(
    bundle_root: Path | BundleSnapshot,
    apply: bool = False,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> dict[str, str]:
    """Plan full index regeneration, including every populated subtree and its ancestors.

    The returned mapping includes unchanged indexes. Application only replaces changed
    destinations. Handwritten index bodies are regenerated, not merged.
    """
    snapshot = load_bundle(bundle_root, limits=limits)
    root = snapshot.root
    documents = {doc.path: doc for doc in snapshot.documents}
    by_dir: dict[Path, list[Document]] = {root: []}
    children: dict[Path, set[Path]] = {}

    for doc in snapshot.documents:
        if doc.fm_error:
            raise BundleError(doc.fm_error, code="invalid_metadata", path=doc.rel)
        directory = doc.path.parent
        by_dir.setdefault(directory, [])
        while directory != root:
            parent = directory.parent
            by_dir.setdefault(parent, [])
            children.setdefault(parent, set()).add(directory)
            directory = parent
        if doc.path.name == "index.md":
            if doc.path.parent != root and doc.frontmatter is not None:
                raise BundleError(
                    "non-root indexes must not have frontmatter",
                    code="invalid_metadata",
                    path=doc.rel,
                )
        if doc.is_reserved:
            continue
        if doc.frontmatter is None or doc.type is None:
            raise BundleError(
                "concept requires valid frontmatter and a non-empty `type`",
                code="invalid_metadata",
                path=doc.rel,
            )
        if "title" in doc.frontmatter and not isinstance(doc.frontmatter["title"], str):
            raise BundleError(
                "`title` must be a string",
                code="invalid_metadata",
                path=doc.rel,
            )
        if "description" in doc.frontmatter and not isinstance(doc.frontmatter["description"], str):
            raise BundleError(
                "`description` must be a string",
                code="invalid_metadata",
                path=doc.rel,
            )
        by_dir[doc.path.parent].append(doc)

    preamble = _preserved_version(documents.get(root / "index.md"))
    out: dict[str, str] = {}
    originals: dict[str, str | None] = {}
    changes: dict[str, str] = {}
    for directory, pages in sorted(by_dir.items()):
        heading = "Knowledge Bundle" if directory == root else directory.name
        lines = [f"# {_escape_text(heading)}", ""]
        for child in sorted(children.get(directory, ())):
            href = quote(child.name, safe="") + "/index.md"
            lines.append(f"* [{_escape_text(child.name)}]({href})")
        for doc in sorted(pages, key=lambda item: item.rel):
            fm = doc.frontmatter
            title = _escape_text(fm.get("title", "").strip() or doc.path.stem)
            description = _escape_text(fm.get("description", "")).strip()
            entry = f"* [{title}]({quote(doc.path.name, safe='')})"
            if description:
                entry += f" - {description}"
            lines.append(entry)
        lines.append("")
        content = (preamble if directory == root else "") + "\n".join(lines)
        index_path = directory / "index.md"
        rel = index_path.relative_to(root).as_posix()
        _, _, error = split_frontmatter(content, limits=limits)
        if error:
            raise BundleError(error, code="invalid_plan", path=rel)
        out[rel] = content
        original = documents.get(index_path)
        original_text = original.raw if original is not None else None
        if original_text != content:
            changes[rel] = content
            originals[rel] = original_text
    if apply:
        apply_changes(root, changes, originals, limits)
    return out
