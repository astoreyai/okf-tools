"""Migrate an Obsidian vault into an OKF-conformant bundle.

Obsidian and OKF are the same architecture: markdown files with YAML frontmatter, one file
per concept, the directory is the graph. They differ on exactly one load-bearing detail.

    Obsidian expresses relationships as  [[wikilinks]]
    OKF permits                          [standard](markdown.md) links, and only those

The spec never mentions wikilink syntax, and the permissive-consumption rule means a
consumer will ingest a wikilink-based vault, report no error, and see **zero relationships**.
So the migration is not cosmetic. It is the difference between shipping a graph and shipping
a pile of disconnected files that looks fine.

Obsidian renders standard markdown links natively (Settings -> Files & Links -> "Use
[[Wikilinks]]" off), so the conversion is not a one-way door: the vault keeps working.

Two rules this migrator will not break
--------------------------------------
1. **An unresolvable wikilink is left alone.** It is a reference to a page that was never
   written. Converting it would produce a dead markdown link, which is strictly worse: OKF
   consumers tolerate broken links in silence, so nothing would ever tell you. Unresolvable
   links stay as-is and are reported as wanted pages.
2. **Nothing is invented.** Recommended fields are derived from data the document already
   carries, and a field with no honest source is simply omitted. A conforming consumer must
   not reject a document for a missing optional field, so omission is always safe and
   fabrication never is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .parser import WIKILINK, iter_documents, split_frontmatter

_FENCE = re.compile(r"```.*?```", re.S)
_CODEISH = re.compile(r'\["|\]\(|-->|=>|\{\{|::|^\w+\[|;\s*$')


@dataclass
class MigrationResult:
    files_changed: int = 0
    links_converted: int = 0
    fields_added: dict[str, int] = field(default_factory=dict)
    unresolved: list[tuple[str, str]] = field(default_factory=list)

    @property
    def wanted_pages(self) -> list[str]:
        seen: list[str] = []
        for _, target in self.unresolved:
            if target not in seen:
                seen.append(target)
        return seen


def _index_targets(bundle_root: Path) -> dict[str, Path]:
    """Resolve a wikilink target by slug, then by title, then by alias. Case-insensitive."""
    idx: dict[str, Path] = {}

    def put(key: str, path: Path) -> None:
        if key and key.strip():
            idx.setdefault(key.strip().lower(), path)

    docs = [d for d in iter_documents(bundle_root) if not d.is_reserved]
    for d in docs:  # slugs first: a filename is unique, a title is not
        put(d.path.stem, d.path)
    for d in docs:
        if not d.frontmatter:
            continue
        put(str(d.frontmatter.get("title") or ""), d.path)
        for alias in d.frontmatter.get("aliases") or []:
            put(str(alias), d.path)
    return idx


def _first_sentence(body: str) -> str:
    """Lift a one-sentence description from prose the document already has.

    Fenced blocks are stripped first. Without that, a vault containing a mermaid diagram
    yields descriptions like `projectfoo123["Foo"]`, which is worse than no description.
    """
    body = _FENCE.sub("", body)
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "|", "-", "*", ">", "```", "---", "→")):
            continue
        if _CODEISH.search(s):
            continue
        s = WIKILINK.sub(lambda m: m.group("alias") or m.group("target"), s)
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        s = re.sub(r"[*_`]", "", s).strip()
        if len(s) < 15:
            continue
        m = re.match(r"(.{15,300}?[.!?])(\s|$)", s)
        return (m.group(1) if m else s[:300]).strip()
    return ""


def migrate(bundle_root: Path, apply: bool = False) -> MigrationResult:
    bundle_root = Path(bundle_root).resolve()
    result = MigrationResult()
    targets = _index_targets(bundle_root)

    for path in sorted(bundle_root.rglob("*.md")):
        if path.name in ("index.md", "log.md"):
            continue
        text = path.read_text(encoding="utf-8")
        fm, body, err = split_frontmatter(text)
        if err:
            continue
        fm = dict(fm or {})
        original_fm, original_body = dict(fm), body

        def repl(m: re.Match) -> str:
            target = m.group("target")
            hit = targets.get(target.strip().lower())
            if hit is None:
                result.unresolved.append((path.relative_to(bundle_root).as_posix(), target))
                return m.group(0)  # never destroy a link we cannot resolve
            result.links_converted += 1
            display = (m.group("alias") or target).strip()
            href = "/" + hit.relative_to(bundle_root).as_posix()
            return f"[{display}]({href})"

        body = WIKILINK.sub(repl, body)

        # Recommended fields, derived only from what the document already carries.
        if not fm.get("description"):
            desc = _first_sentence(body)
            if desc:
                fm["description"] = desc
                result.fields_added["description"] = result.fields_added.get("description", 0) + 1
        if not fm.get("timestamp"):
            stamp = fm.get("updated") or fm.get("created") or fm.get("date")
            if stamp:
                fm["timestamp"] = str(stamp)
                result.fields_added["timestamp"] = result.fields_added.get("timestamp", 0) + 1

        if body != original_body or fm != original_fm:
            result.files_changed += 1
            if apply:
                for k, v in list(fm.items()):
                    if isinstance(v, str):
                        fm[k] = " ".join(v.split())
                dumped = yaml.safe_dump(
                    fm, sort_keys=False, allow_unicode=True,
                    default_flow_style=False, width=10_000,
                )
                path.write_text("---\n" + dumped + "---" + body, encoding="utf-8")

    return result
