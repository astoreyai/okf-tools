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

The `generated` question
------------------------
v0.2 §13.1 supersedes `timestamp` with `generated: { by, at }`, and §5.2 makes `by` REQUIRED
within it. A vault carries a modification date but no actor: the file does not record who or
what wrote it, and this tool did not. Inventing one would break rule 2 above, so the actor is
something only you can supply.

    okf migrate ./vault --actor human:aaron --apply

With `--actor`, a full `generated` is written. Without it, the derived date is written as the
legacy `timestamp`, which §13.1 explicitly permits a v0.2 consumer to fall back to. The
default is therefore always spec-legal and never fabricates an author.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import yaml
from markdown_it import MarkdownIt

from .parser import (
    BundleSnapshot,
    _index_targets,
    _resolve_target,
    is_conventional_actor,
    iter_wikilinks,
    load_bundle,
    split_frontmatter,
)
from .safety import DEFAULT_LIMITS, BundleError, Limits, apply_changes

_MARKDOWN = MarkdownIt("commonmark")
_MARKDOWN_PUNCTUATION = re.compile(r"([\\`*{}\[\]<>()#+.!_|~\-])")


@dataclass
class MigrationResult:
    files_changed: int = 0
    links_converted: int = 0
    fields_added: dict[str, int] = field(default_factory=dict)
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    changes: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    applied: list[str] = field(default_factory=list)

    @property
    def wanted_pages(self) -> list[str]:
        return list(dict.fromkeys(target for _, target in self.unresolved))


def _metadata_error(fm: dict[str, Any]) -> str | None:
    if any(not isinstance(key, str) for key in fm):
        return "frontmatter keys must be strings"
    if not isinstance(fm.get("type"), str) or not fm["type"].strip():
        return "missing, empty, or non-string `type`; migration never guesses a type"
    for key in ("title", "description"):
        if key in fm and not isinstance(fm[key], str):
            return f"`{key}` must be a string"
    if "aliases" in fm and (
        not isinstance(fm["aliases"], list)
        or any(not isinstance(alias, str) or not alias.strip() for alias in fm["aliases"])
    ):
        return "`aliases` must be a list of non-empty strings"
    for key in ("generated", "verified"):
        if key not in fm:
            continue
        value = fm[key]
        events = value if key == "verified" and isinstance(value, list) else [value]
        for event in events:
            if not isinstance(event, dict):
                return f"`{key}` must contain actor-event mappings"
            if not isinstance(event.get("by"), str) or not event["by"].strip():
                return f"`{key}.by` must be a non-empty string actor identity"
    if "sources" in fm:
        value = fm["sources"]
        sources = [value] if isinstance(value, dict) else value
        if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
            return "`sources` must be a mapping or a list of mappings"
        for source in sources:
            if "author" in source and (
                not isinstance(source["author"], str) or not source["author"].strip()
            ):
                return "`sources[].author` must be a non-empty string actor identity"
    return None


def _escape_label(value: str) -> str:
    return _MARKDOWN_PUNCTUATION.sub(r"\\\1", " ".join(value.splitlines()))


def _first_sentence(body: str) -> str:
    """Lift existing prose, never fenced/indented code or a heading, as a description."""
    tokens = _MARKDOWN.parse(body)
    for index, token in enumerate(tokens):
        if token.type != "paragraph_open" or token.level != 0:
            continue
        inline = tokens[index + 1]
        if inline.type != "inline":
            continue
        parts = []
        for child in inline.children or []:
            if child.type in ("text", "code_inline"):
                parts.append(child.content)
            elif child.type in ("softbreak", "hardbreak"):
                parts.append(" ")
        prose = "".join(parts).strip()
        if len(prose) < 15:
            continue
        match = re.match(r"(.{15,300}?[.!?])(?:\s|$)", prose)
        return (match.group(1) if match else prose[:300]).strip()
    return ""


def migrate(
    bundle_root: Path | BundleSnapshot,
    apply: bool = False,
    actor: str | None = None,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> MigrationResult:
    """Plan conservative edits, then apply the complete plan through guarded writes."""
    if actor is not None and not is_conventional_actor(actor):
        raise BundleError("actor must be a non-empty conventional identity", code="invalid_actor")
    snapshot = load_bundle(bundle_root, limits=limits)
    result = MigrationResult()
    documents = [doc for doc in snapshot.documents if not doc.is_reserved]
    targets = _index_targets(documents)
    paths = {doc.rel for doc in documents}
    folded_paths: dict[str, set[str]] = {}
    for path in paths:
        folded_paths.setdefault(path.casefold(), set()).add(path)
    originals: dict[str, str | None] = {}
    diagnostics = 0

    for doc in documents:
        reason = doc.fm_error
        if not reason:
            reason = _metadata_error(doc.frontmatter or {})
        if reason:
            result.skipped[doc.rel] = reason
            diagnostics += 1
            if diagnostics > limits.max_findings:
                raise BundleError("migration diagnostic limit exceeded", code="resource_limit")
            continue
        fm = dict(doc.frontmatter or {})
        chunks: list[str] = []
        cursor = 0
        converted = 0
        for match in iter_wikilinks(doc.body, limits=limits):
            # An embed is content inclusion, not an ordinary concept relationship.
            if match.start() and doc.body[match.start() - 1] == "!":
                continue
            target = match.group("target")
            hits, fragment = _resolve_target(target, doc, paths, folded_paths, targets)
            if len(hits) != 1:
                diagnostics += 1
                if diagnostics > limits.max_findings:
                    raise BundleError("migration diagnostic limit exceeded", code="resource_limit")
                if hits:
                    candidates = set(result.ambiguous.get(target, ()))
                    result.ambiguous[target] = sorted(candidates.union(hits))
                else:
                    result.unresolved.append((doc.rel, target))
                continue
            display = (match.group("alias") or target).strip()
            href = "/" + quote(hits[0], safe="/")
            if "#" in target:
                href += "#" + quote(unquote(fragment), safe="")
            chunks.extend((doc.body[cursor : match.start()], f"[{_escape_label(display)}]({href})"))
            cursor = match.end()
            converted += 1
        if converted:
            chunks.append(doc.body[cursor:])
            body = "".join(chunks)
        else:
            body = doc.body

        added: list[str] = []
        # Key presence, not truthiness: even an empty extension scalar is the author's.
        if "description" not in fm:
            description = _first_sentence(body)
            if description:
                fm["description"] = description
                added.append("description")
        if "generated" not in fm:
            stamp = next(
                (
                    fm[key]
                    for key in ("updated", "created", "date")
                    if isinstance(fm.get(key), (str, date, datetime)) and str(fm[key]).strip()
                ),
                None,
            )
            if actor is not None:
                generated = {"by": actor}
                if stamp is not None:
                    generated["at"] = str(stamp)
                fm["generated"] = generated
                added.append("generated")
            elif stamp is not None and "timestamp" not in fm:
                fm["timestamp"] = str(stamp)
                added.append("timestamp")

        if not added and not converted:
            continue
        if added:
            newline = "\r\n" if doc.raw.startswith("---\r\n") else "\n"
            try:
                dumped = yaml.safe_dump(
                    fm,
                    sort_keys=False,
                    allow_unicode=True,
                    default_flow_style=False,
                    width=10_000,
                    line_break=newline,
                )
            except yaml.YAMLError as exc:
                raise BundleError(
                    "cannot serialize migration frontmatter", code="invalid_plan", path=doc.rel
                ) from exc
            content = "---" + newline + dumped + "---" + newline + body
        else:
            content = doc.raw[: len(doc.raw) - len(doc.body)] + body
        planned_fm, planned_body, error = split_frontmatter(content, limits=limits)
        if error or planned_fm is None or planned_body != body:
            raise BundleError(
                error or "migration plan did not preserve its document body",
                code="invalid_plan",
                path=doc.rel,
            )
        result.changes[doc.rel] = content
        originals[doc.rel] = doc.raw
        result.links_converted += converted
        for key in added:
            result.fields_added[key] = result.fields_added.get(key, 0) + 1

    result.files_changed = len(result.changes)
    if apply:
        result.applied = apply_changes(snapshot.root, result.changes, originals, limits)
    return result
