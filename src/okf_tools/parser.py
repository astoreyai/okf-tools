"""Frontmatter and link parsing for Open Knowledge Format bundles.

The parser here is deliberately strict about one thing: **frontmatter ends at a LINE
that is exactly `---`, never at the first `---` substring.**

That distinction is not pedantry. A concept titled "etl - Nightly Loader" slugifies to
`etl---nightly-loader`, and a producer that stamps the slug into an `id` field emits a
perfectly valid document whose frontmatter *contains* `---`. A parser that does
`text.split("---")` cuts that document in half and reports it as malformed, sending you
hunting for a corruption that was never there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import yaml

RESERVED_NAMES = {"index.md", "log.md"}

# Standard markdown links. OKF permits these and only these.
MD_LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<href>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
# Obsidian wikilinks. NOT in the spec: an OKF consumer sees these as literal text.
WIKILINK = re.compile(r"\[\[(?P<target>[^\]|]+?)(?:\|(?P<alias>[^\]]+?))?\]\]")


@dataclass
class Link:
    text: str
    href: str
    line: int

    @property
    def is_external(self) -> bool:
        return self.href.startswith(("http://", "https://", "mailto:"))

    @property
    def is_anchor(self) -> bool:
        return self.href.startswith("#")


@dataclass
class Document:
    path: Path
    bundle_root: Path
    raw: str
    frontmatter: dict[str, Any] | None
    body: str
    fm_error: str | None = None
    links: list[Link] = field(default_factory=list)
    wikilinks: list[str] = field(default_factory=list)

    @property
    def rel(self) -> str:
        return self.path.relative_to(self.bundle_root).as_posix()

    @property
    def is_reserved(self) -> bool:
        return self.path.name in RESERVED_NAMES

    @property
    def type(self) -> str | None:
        if not self.frontmatter:
            return None
        t = self.frontmatter.get("type")
        return t if isinstance(t, str) and t.strip() else None

    def resolve(self, link: Link) -> Path | None:
        """Resolve a link to a path inside the bundle, or None if it points outside it.

        OKF permits two forms: absolute (leading `/`, relative to the bundle root) and
        relative (ordinary markdown relative paths).
        """
        if link.is_external or link.is_anchor:
            return None
        href = link.href.split("#", 1)[0]
        if not href:
            return None
        root = self.bundle_root.resolve()
        if href.startswith("/"):
            return root / href.lstrip("/")
        return (self.path.parent.resolve() / href).resolve()


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    """Return (frontmatter, body, error).

    frontmatter is None when the document has none (which is REQUIRED for reserved
    files and a conformance failure for concept documents).
    """
    if not text.startswith("---"):
        return None, text, None
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return None, text, None

    for i in range(1, len(lines)):
        if lines[i].strip() == "---":  # a LINE equal to ---, not a substring
            block = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            try:
                fm = yaml.safe_load(block)
            except yaml.YAMLError as exc:
                first = str(exc).split("\n")[0]
                return None, body, f"unparseable YAML: {first}"
            if fm is None:
                return {}, body, None
            if not isinstance(fm, dict):
                return None, body, "frontmatter is not a mapping"
            return fm, body, None

    return None, text, "frontmatter opened with --- but never closed"


def parse_document(path: Path, bundle_root: Path) -> Document:
    bundle_root = Path(bundle_root).resolve()
    path = Path(path).resolve()
    raw = path.read_text(encoding="utf-8")
    fm, body, err = split_frontmatter(raw)
    doc = Document(
        path=path, bundle_root=bundle_root, raw=raw,
        frontmatter=fm, body=body, fm_error=err,
    )
    offset = raw.count("\n", 0, len(raw) - len(body)) if body and body != raw else 0
    for i, line in enumerate(body.split("\n"), start=offset + 1):
        for m in MD_LINK.finditer(line):
            doc.links.append(Link(text=m.group("text"), href=m.group("href"), line=i))
        for m in WIKILINK.finditer(line):
            doc.wikilinks.append(m.group("target"))
    return doc


def iter_documents(bundle_root: Path) -> Iterator[Document]:
    bundle_root = Path(bundle_root).resolve()
    for p in sorted(bundle_root.rglob("*.md")):
        yield parse_document(p, bundle_root)


def concepts(bundle_root: Path) -> list[Document]:
    return [d for d in iter_documents(bundle_root) if not d.is_reserved]


def reserved(bundle_root: Path) -> list[Document]:
    return [d for d in iter_documents(bundle_root) if d.is_reserved]
