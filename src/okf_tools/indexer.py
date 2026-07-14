"""Generate OKF reserved `index.md` files (progressive disclosure).

Per the spec, an `index.md` MAY appear in any directory, contains **no frontmatter**, and
its body is sections of `* [Title](url) - description`. It is a reserved filename and MUST
NOT be used for a concept document. Producers MAY generate it; consumers MAY synthesize one
when it is absent.

Note that a file named `_index.md` (leading underscore) has no meaning in OKF. It is an
ordinary concept document. Vaults that use `_index.md` as a folder note can keep them: the
two coexist without conflict.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .parser import parse_document

RESERVED = ("index.md", "log.md")


def build_indexes(bundle_root: Path, apply: bool = False) -> dict[str, str]:
    """Return {relative path -> index.md content}. Writes them when apply is True."""
    bundle_root = Path(bundle_root).resolve()
    by_dir: dict[Path, list[Path]] = defaultdict(list)
    for p in sorted(bundle_root.rglob("*.md")):
        if p.name in RESERVED or p.name.startswith("_"):
            continue
        by_dir[p.parent].append(p)

    out: dict[str, str] = {}

    for directory, pages in sorted(by_dir.items()):
        heading = "Knowledge Bundle" if directory == bundle_root else directory.name
        lines = [f"# {heading}", ""]
        for p in pages:
            doc = parse_document(p, bundle_root)
            fm = doc.frontmatter or {}
            title = str(fm.get("title") or p.stem)
            desc = str(fm.get("description") or "").strip()
            href = p.name  # relative, matching the reference bundles
            entry = f"* [{title}]({href})"
            if desc:
                entry += f" - {desc}"
            lines.append(entry)
        lines.append("")
        content = "\n".join(lines)
        rel = (directory / "index.md").relative_to(bundle_root).as_posix()
        out[rel] = content
        if apply:
            (directory / "index.md").write_text(content, encoding="utf-8")

    return out
