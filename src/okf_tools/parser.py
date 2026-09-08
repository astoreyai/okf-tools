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

import posixpath
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit

import yaml
from markdown_it import MarkdownIt
from markdown_it.rules_block import reference as _reference_rule
from markdown_it.rules_inline import autolink as _autolink_rule
from markdown_it.rules_inline import image as _image_rule
from markdown_it.rules_inline import html_inline as _html_rule, link as _link_rule
from markdown_it.token import Token

from .safety import DEFAULT_LIMITS, BundleError, Limits
from .safety import bundle_root as checked_root
from .safety import markdown_paths, read_text

RESERVED_NAMES = {"index.md", "log.md"}

# Obsidian wikilinks. NOT in the spec: an OKF consumer sees these as literal text.
WIKILINK = re.compile(r"\[\[(?P<target>[^\[\]|\r\n]+)(?:\|(?P<alias>[^\[\]\r\n]+))?\]\]")
# Markdown footnotes. v0.2 §5.1 makes these the per-claim attribution mechanism: a
# footnote whose label is a `sources[].id` attributes that claim to that source.
FOOTNOTE_DEF = re.compile(r"^\[\^(?P<label>[^\[\]\r\n]+)\]:")
FOOTNOTE_REF = re.compile(r"\[\^(?P<label>[^\[\]\r\n]+)\]")

# v0.2 §7 actor convention: `<producer>/<version>`, `human:<id>`, `process:<id>`.
# `team:<id>` is not in §7 but is used for `sources[].author` throughout the reference
# bundles, so it is accepted rather than flagged.
ACTOR_PREFIXES = ("human:", "process:", "team:")

# A path-valued field (§6.2) that could instead be a scope descriptor (§5.1) is only
# treated as a path when it looks like a file: no whitespace and a real extension.
# `dashboards/exec-revenue` (a source in the spec's own worked example) is not a file.
_HAS_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


@dataclass
class Link:
    text: str
    href: str
    line: int

    @property
    def is_external(self) -> bool:
        return bool(_SCHEME.match(self.href)) or self.href.startswith("//")

    @property
    def is_anchor(self) -> bool:
        return self.href.startswith("#")


@dataclass
class PathField:
    """A frontmatter field holding a path (§6.2), which is a link nothing else checks."""

    label: str
    value: str
    strict: bool  # strict fields are always paths; loose ones may be scope descriptors


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
    footnote_refs: list[tuple[str, int]] = field(default_factory=list)
    footnote_defs: set[str] = field(default_factory=set)
    headings: list[tuple[int, str, int]] = field(default_factory=list)
    computation_fences: int = 0

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

    @property
    def source_ids(self) -> set[str]:
        """The `id` of every `sources` entry (§5.1). These are footnote join keys."""
        out: set[str] = set()
        for entry in self.sources:
            sid = entry.get("id")
            if isinstance(sid, str) and sid.strip():
                out.add(sid.strip())
        return out

    @property
    def sources(self) -> list[dict[str, Any]]:
        if not self.frontmatter:
            return []
        raw = self.frontmatter.get("sources")
        if isinstance(raw, dict):  # a single source written without the list dash
            return [raw]
        if isinstance(raw, list):
            return [e for e in raw if isinstance(e, dict)]
        return []

    @property
    def aliases(self) -> list[str]:
        """Only list-valued string aliases participate in name resolution."""
        raw = (self.frontmatter or {}).get("aliases")
        return (
            [v.strip() for v in raw if isinstance(v, str) and v.strip()]
            if isinstance(raw, list)
            else []
        )

    def path_fields(self) -> list[PathField]:
        """Every frontmatter field naming a path (§6.2).

        These are links in everything but syntax: `attester.resource` pointing at a file
        that does not exist severs the attestation chain exactly the way a broken markdown
        link severs a relationship, and nothing in OKF requires a consumer to notice.
        """
        fm = self.frontmatter or {}
        out: list[PathField] = []

        def add(label: str, value: Any, strict: bool) -> None:
            if isinstance(value, str) and value.strip():
                out.append(PathField(label, value.strip(), strict))

        add("resource", fm.get("resource"), strict=False)
        add("computation", fm.get("computation"), strict=True)
        for key in ("executor", "attester"):
            block = fm.get(key)
            if isinstance(block, dict):
                add(f"{key}.resource", block.get("resource"), strict=True)
        for i, entry in enumerate(self.sources):
            add(f"sources[{i}].resource", entry.get("resource"), strict=False)
        return out

    def resolve_path_field(self, pf: PathField) -> Path | None:
        """Resolve local paths, retaining the documented bare root-relative fallback.

        Explicit relative paths never use that fallback. Escapes are returned before
        existence checks; two distinct existing bare candidates are an ambiguity.
        """
        value = pf.value
        if _SCHEME.match(value) or value.startswith(("//", "#")):
            return None
        if not pf.strict and any(c.isspace() for c in value):
            return None
        target = _url_path(value)
        if not target or (not pf.strict and not _HAS_SUFFIX.search(target)):
            return None
        root = self.bundle_root
        if target.startswith("/"):
            return _canonical(root / target.lstrip("/"))
        local = _canonical(self.path.parent / target)
        if not _inside(local, root) or target.startswith(("./", "../")):
            return local
        fallback = _canonical(root / target)
        if not _inside(fallback, root):
            return fallback
        if local == fallback:
            return local
        local_exists, fallback_exists = local.exists(), fallback.exists()
        if local_exists and fallback_exists:
            raise BundleError(
                f"`{pf.label}` has both document-relative and root-relative targets: "
                f"{local.relative_to(root)} and {fallback.relative_to(root)}",
                code="ambiguous_path",
                path=self.rel,
            )
        return fallback if fallback_exists else local

    def resolve(self, link: Link) -> Path | None:
        """Return a canonical local target, including escapes for callers to diagnose."""
        if link.is_external or link.is_anchor:
            return None
        href = _url_path(link.href)
        if not href:
            return None
        base = self.bundle_root if href.startswith("/") else self.path.parent
        return _canonical(base / href.lstrip("/"))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _canonical(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise BundleError(str(exc), code="unsafe_path", path=path) from exc


def _url_path(value: str) -> str:
    try:
        return unquote(urlsplit(value).path)
    except ValueError as exc:
        raise BundleError(str(exc), code="unsafe_path", path=value) from exc


def split_frontmatter(
    text: str,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> tuple[dict[str, Any] | None, str, str | None]:
    """Return (frontmatter, exact body, error), with bounded YAML construction."""
    _check_text(text, limits)
    lines = [line for line in re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", text) if line]
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, text, None
    end = len(lines[0])
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            block = text[len(lines[0]) : end]
            body = text[end + len(lines[i]) :]
            try:
                fm = _load_yaml(block, limits)
            except (yaml.YAMLError, ValueError) as exc:
                if isinstance(exc, BundleError):
                    raise
                return None, body, f"unparseable YAML: {str(exc).splitlines()[0]}"
            if fm is None:
                return {}, body, None
            if not isinstance(fm, dict):
                return None, body, "frontmatter is not a mapping"
            return fm, body, None
        end += len(lines[i])
    return None, text, "frontmatter opened with --- but never closed"


def _check_text(text: str, limits: Limits) -> None:
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise BundleError("text is not valid UTF-8", code="parse_error") from exc
    if size > limits.max_file_bytes:
        raise BundleError("file byte limit exceeded", code="resource_limit")
    if any(len(line.encode("utf-8")) > limits.max_line_bytes for line in text.splitlines()):
        raise BundleError("line byte limit exceeded", code="resource_limit")


def _load_yaml(block: str, limits: Limits) -> Any:
    # Check the composed graph before construction: merge aliases may otherwise
    # expand exponentially even though the YAML input itself is small.
    maximum = min(limits.max_file_bytes, 100_000)

    class BoundedLoader(yaml.SafeLoader):
        depth = 0
        nodes = 0

        def compose_node(self, parent, index):
            self.depth += 1
            self.nodes += 1
            try:
                if self.depth > limits.max_depth or self.nodes > maximum:
                    raise BundleError("YAML structure limit exceeded", code="resource_limit")
                return super().compose_node(parent, index)
            finally:
                self.depth -= 1

    loader = BoundedLoader(block)
    sizes: dict[int, int] = {}
    active: set[int] = set()

    def measure(node, depth: int) -> int:
        ident = id(node)
        if ident in active:
            raise yaml.YAMLError("recursive YAML aliases are not supported")
        if depth > limits.max_depth:
            raise BundleError("YAML depth limit exceeded", code="resource_limit")
        if ident in sizes:
            return sizes[ident]
        active.add(ident)
        size = 1
        children = ()
        if isinstance(node, yaml.MappingNode):
            children = (child for pair in node.value for child in pair)
        elif isinstance(node, yaml.SequenceNode):
            children = iter(node.value)
        for child in children:
            size += measure(child, depth + 1)
            if size > maximum:
                raise BundleError("YAML expansion limit exceeded", code="resource_limit")
        active.remove(ident)
        sizes[ident] = size
        return size

    try:
        node = loader.get_single_node()
        if node is None:
            return None
        measure(node, 0)
        return loader.construct_document(node)
    except (RecursionError, MemoryError) as exc:
        raise BundleError("YAML parser resource limit exceeded", code="resource_limit") from exc
    finally:
        loader.dispose()


def normalize_verified(frontmatter: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return `verified` as a list of events (§5.2).

    v0.2 §11 makes this a consumer MUST: a single verifier may be written as a bare
    `{ by, at }` mapping without the list dash, and a consumer that only handles the list
    form silently sees a human-reviewed concept as unverified.
    """
    if not frontmatter:
        return []
    verified = frontmatter.get("verified")
    if isinstance(verified, dict):
        return [verified]
    if isinstance(verified, list):
        return [v for v in verified if isinstance(v, dict)]
    return []


def trust_tier(frontmatter: dict[str, Any] | None) -> str:
    """Derive a trust tier from `verified` (§5.3), lowest to highest."""
    events = [
        event
        for event in normalize_verified(frontmatter)
        if isinstance(event.get("by"), str) and event["by"].strip()
    ]
    if not events:
        return "unverified"
    for event in events:
        by = event.get("by")
        if by.strip().startswith("human:") and is_conventional_actor(by):
            return "human-reviewed"
    return "machine-confirmed"


def is_conventional_actor(value: Any) -> bool:
    """Whether a value matches the §7 actor convention."""
    if not isinstance(value, str) or not value.strip():
        return False
    actor = value.strip()
    if any(c.isspace() for c in actor):
        return False
    for prefix in ACTOR_PREFIXES:
        if actor.startswith(prefix):
            return bool(actor[len(prefix) :])
    producer, separator, version = actor.partition("/")
    return bool(separator and producer and version)


def _index_targets(documents: list[Document]) -> dict[str, set[str]]:
    """Keep every candidate; a shared stem, title, or alias is not a unique identity."""
    targets: dict[str, set[str]] = {}
    for doc in documents:
        keys = [doc.path.stem]
        fm = doc.frontmatter or {}
        if isinstance(fm.get("title"), str):
            keys.append(fm["title"])
        aliases = fm.get("aliases")
        if isinstance(aliases, list):
            keys.extend(alias for alias in aliases if isinstance(alias, str))
        for key in keys:
            key = key.strip().casefold()
            if key:
                targets.setdefault(key, set()).add(doc.rel)
    return targets


def _resolve_target(
    target: str,
    source: Document,
    paths: set[str],
    folded_paths: dict[str, set[str]],
    targets: dict[str, set[str]],
) -> tuple[list[str], str]:
    """Prefer explicit/local paths; global name matching must be unambiguous."""
    name, separator, fragment = target.strip().partition("#")
    name = unquote(name).strip()
    if not name:
        return ([source.rel] if separator else []), fragment
    if "\\" in name or "\x00" in name:
        return [], fragment

    def candidates(path: str) -> list[str]:
        path = posixpath.normpath(path)
        if path == ".." or path.startswith("../") or path.startswith("/"):
            return []
        if not path.lower().endswith(".md"):
            path += ".md"
        if path in paths:
            return [path]
        return sorted(folded_paths.get(path.casefold(), ()))

    parent = posixpath.dirname(source.rel)
    if name.startswith("/"):
        return candidates(name.lstrip("/")), fragment
    if name.startswith(("./", "../")):
        return candidates(posixpath.join(parent, name)), fragment
    local = candidates(posixpath.join(parent, name))
    if local:
        return local, fragment
    if "/" in name:
        return candidates(name), fragment
    key = name[:-3] if name.lower().endswith(".md") else name
    return sorted(targets.get(key.casefold(), ())), fragment


@dataclass
class BundleSnapshot:
    root: Path
    documents: list[Document]


class _HTMLContext(HTMLParser):
    """Retain HTML exclusion across inline tokens and paragraph boundaries."""

    def __init__(self, max_depth: int) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.max_depth = max_depth

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in _VOID_TAGS:
            if len(self.stack) >= self.max_depth:
                raise BundleError("HTML nesting limit exceeded", code="resource_limit")
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        pass

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] == tag:
                del self.stack[index:]
                break


def _marks(state, silent: bool) -> bool:
    """Recognize nonstandard prose marks using the real inline parser context."""
    if state.src[state.pos] != "[":
        return False
    match = WIKILINK.match(state.src, state.pos, state.posMax)
    kind = "wikilink"
    if match is None:
        match = FOOTNOTE_REF.match(state.src, state.pos, state.posMax)
        kind = "footnote"
    if match is None:
        return False
    if not silent:
        literal = (
            state.env["okf_html"].stack
            or state.linkLevel
            or (state.pos > 0 and state.src[state.pos - 1] == "!")
        )
        token = state.push("text" if literal else f"okf_{kind}", "", 0)
        token.content = match.group()
        token.meta["start"] = state.pos
        token.meta["end"] = match.end()
    state.pos = match.end()
    return True


def _html(state, silent: bool) -> bool:
    start = state.pos
    if not _html_rule(state, silent):
        return False
    if not silent:
        state.env["okf_html"].feed(state.src[start : state.pos])
    return True


def _link(state, silent: bool, rule=_link_rule) -> bool:
    start = state.pos
    first = len(state.tokens)
    literal = bool(state.env["okf_html"].stack)
    if not rule(state, silent):
        return False
    if not silent:
        for token in state.tokens[first:]:
            if token.type == "link_open":
                token.meta["start"] = start
                token.meta["literal"] = literal
                break
    return True


def _autolink(state, silent: bool) -> bool:
    return _link(state, silent, _autolink_rule)


def _image(state, silent: bool) -> bool:
    # Image alt text is parsed recursively but its HTML is not document HTML.
    context = state.env["okf_html"]
    state.env["okf_html"] = _HTMLContext(context.max_depth)
    try:
        return _image_rule(state, silent)
    finally:
        state.env["okf_html"] = context


def _reference(state, start: int, end: int, silent: bool) -> bool:
    pos = state.bMarks[start] + state.tShift[start]
    if state.src.startswith("[^", pos):
        return False  # footnotes are attribution, not CommonMark link definitions
    return _reference_rule(state, start, end, silent)


def _markdown(body: str, limits: Limits):
    _check_text(body, limits)
    md = MarkdownIt("commonmark", {"maxNesting": limits.max_depth})
    md.inline.ruler.after("link", "okf_marks", _marks)
    md.inline.ruler.at("html_inline", _html)
    md.inline.ruler.at("link", _link)
    md.inline.ruler.at("autolink", _autolink)
    md.inline.ruler.at("image", _image)
    md.block.ruler.at("reference", _reference)
    # Source lines retain exact original offsets, including CRLF. markdown-it's
    # block content removes container prefixes, so map each content line back to
    # its source line rather than treating token offsets as file offsets.
    lines = [line for line in re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", body) if line]
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    tokens: list[Token] = []
    env: dict[str, Any] = {"okf_html": _HTMLContext(limits.max_depth)}
    links: list[Link] = []
    wiki: list[re.Match] = []
    refs: list[tuple[str, int]] = []
    definitions: set[str] = set()
    headings: list[tuple[int, str, int]] = []
    computation_fences = 0
    computation_section = False
    mark_count = 0
    try:
        md.block.parse(body.replace("\r\n", "\n").replace("\r", "\n"), md, env, tokens)
        for index, token in enumerate(tokens):
            if token.type == "html_block":
                env["okf_html"].feed(token.content)
            if token.type == "heading_open":
                following = tokens[index + 1]
                level = int(token.tag[1:])
                headings.append((level, following.content, token.map[0] + 1))
                if level == 1:
                    computation_section = following.content.strip() == "Computation"
            if token.type == "fence" and computation_section and token.content.strip():
                computation_fences += 1
            if token.type != "inline" or token.map is None:
                continue
            children: list[Token] = []
            md.inline.parse(token.content, md, env, children)
            if not any("start" in child.meta for child in children):
                continue
            content_lines = token.content.split("\n")
            content_starts = [0]
            locations: list[tuple[int, int]] = []
            cursor = token.map[0]
            for part in content_lines:
                content_starts.append(content_starts[-1] + len(part) + 1)
                while cursor < min(token.map[1], len(lines)):
                    content = part.lstrip(" \t")
                    column = lines[cursor].find(content)
                    if column >= 0:
                        # A partially consumed container-indentation tab becomes
                        # spaces in token.content, not in the original source.
                        locations.append((cursor, column - (len(part) - len(content))))
                        cursor += 1
                        break
                    cursor += 1
                else:
                    raise BundleError("cannot map Markdown source positions", code="parse_error")
            for child_index, child in enumerate(children):
                if "start" not in child.meta:
                    continue
                start = child.meta["start"]
                line_index = bisect_right(content_starts, start) - 1
                row, column = locations[line_index]
                absolute = starts[row] + column + start - content_starts[line_index]
                if child.type == "okf_wikilink":
                    match = WIKILINK.match(body, absolute)
                    if match is None:
                        raise BundleError(
                            "cannot map wikilink source positions", code="parse_error"
                        )
                    wiki.append(match)
                elif child.type == "okf_footnote":
                    label = child.content[2:-1].strip()
                    prefix = token.content[token.content.rfind("\n", 0, start) + 1 : start]
                    if not prefix.strip() and token.content.startswith(":", child.meta["end"]):
                        definitions.add(label)
                    else:
                        refs.append((label, row + 1))
                elif child.type == "link_open" and not child.meta.get("literal"):
                    label_parts = []
                    for nested_index in range(child_index + 1, len(children)):
                        nested = children[nested_index]
                        if nested.type == "link_close":
                            break
                        label_parts.append(nested.content)
                    links.append(
                        Link("".join(label_parts), str(child.attrGet("href") or ""), row + 1)
                    )
                else:
                    continue
                mark_count += 1
                if mark_count > limits.max_findings:
                    raise BundleError("Markdown link limit exceeded", code="resource_limit")
        return links, wiki, refs, definitions, headings, computation_fences
    except (RecursionError, MemoryError) as exc:
        raise BundleError("Markdown parser resource limit exceeded", code="resource_limit") from exc


def iter_wikilinks(body: str, *, limits: Limits = DEFAULT_LIMITS) -> Iterator[re.Match]:
    """Yield original-body matches in prose, excluding code, escapes, HTML and embeds."""
    yield from _markdown(body, limits)[1]


def _parse_document(path: Path, root: Path, limits: Limits) -> Document:
    raw = read_text(path, root, limits)
    fm, body, err = split_frontmatter(raw, limits=limits)
    links, wiki, refs, definitions, headings, fences = _markdown(body, limits)
    offset = len(re.findall(r"\r\n|\r|\n", raw[: len(raw) - len(body)]))
    for link in links:
        link.line += offset
    return Document(
        path=path,
        bundle_root=root,
        raw=raw,
        frontmatter=fm,
        body=body,
        fm_error=err,
        links=links,
        wikilinks=[m.group("target") for m in wiki],
        footnote_refs=[(label, line + offset) for label, line in refs],
        footnote_defs=definitions,
        headings=[(level, title, line + offset) for level, title, line in headings],
        computation_fences=fences,
    )


def parse_document(
    path: Path,
    bundle_root: Path,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> Document:
    root = checked_root(bundle_root)
    # Do not resolve the descendant: read_text must see and reject symlinks.
    candidate = Path(path).absolute()
    try:
        candidate = root / candidate.relative_to(Path(bundle_root).absolute())
    except ValueError:
        pass  # It may already be rooted at the canonical bundle path.
    return _parse_document(candidate, root, limits)


def load_bundle(
    path: Path | BundleSnapshot,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> BundleSnapshot:
    if isinstance(path, BundleSnapshot):
        root = checked_root(path.root)
        if root != path.root or not isinstance(path.documents, list):
            raise BundleError(
                "snapshot must have a canonical root and document list", code="invalid_snapshot"
            )
        if len(path.documents) > limits.max_files:
            raise BundleError("snapshot file limit exceeded", code="resource_limit")
        total = 0
        seen: set[Path] = set()
        for document in path.documents:
            if (
                not isinstance(document, Document)
                or not isinstance(document.path, Path)
                or document.bundle_root != root
                or not _inside(document.path, root)
                or ".." in document.path.parts
                or document.path in seen
            ):
                raise BundleError(
                    "snapshot document paths must be unique and within its root",
                    code="invalid_snapshot",
                )
            relative = document.path.relative_to(root)
            if not relative.parts or document.path.suffix != ".md":
                raise BundleError(
                    "snapshot document must name a Markdown file",
                    code="invalid_snapshot",
                    path=document.path,
                )
            if len(relative.parts) - 1 > limits.max_depth:
                raise BundleError("snapshot depth limit exceeded", code="resource_limit")
            if not isinstance(document.raw, str) or not isinstance(document.body, str):
                raise BundleError("snapshot document text must be strings", code="invalid_snapshot")
            _check_text(document.raw, limits)
            _check_text(document.body, limits)
            if document.frontmatter is not None and not isinstance(document.frontmatter, dict):
                raise BundleError(
                    "snapshot frontmatter must be a mapping or absent", code="invalid_snapshot"
                )
            pending = [(document.frontmatter, 0)]
            nodes = 0
            while pending:
                value, depth = pending.pop()
                nodes += 1
                if depth > limits.max_depth or nodes > min(limits.max_file_bytes, 100_000):
                    raise BundleError(
                        "snapshot YAML structure limit exceeded", code="resource_limit"
                    )
                if isinstance(value, dict):
                    pending.extend((child, depth + 1) for child in value.values())
                elif isinstance(value, (list, tuple, set)):
                    pending.extend((child, depth + 1) for child in value)
            if (
                not isinstance(document.links, list)
                or not all(
                    isinstance(link, Link)
                    and isinstance(link.href, str)
                    and isinstance(link.text, str)
                    and isinstance(link.line, int)
                    and not isinstance(link.line, bool)
                    and link.line > 0
                    for link in document.links
                )
                or not isinstance(document.wikilinks, list)
                or not all(isinstance(link, str) for link in document.wikilinks)
                or not isinstance(document.footnote_refs, list)
                or not all(
                    isinstance(ref, (tuple, list))
                    and len(ref) == 2
                    and isinstance(ref[0], str)
                    and isinstance(ref[1], int)
                    and not isinstance(ref[1], bool)
                    and ref[1] > 0
                    for ref in document.footnote_refs
                )
                or not isinstance(document.footnote_defs, set)
                or not all(isinstance(label, str) for label in document.footnote_defs)
                or not isinstance(document.headings, list)
                or not all(
                    isinstance(heading, (tuple, list))
                    and len(heading) == 3
                    and isinstance(heading[0], int)
                    and 1 <= heading[0] <= 6
                    and isinstance(heading[1], str)
                    and isinstance(heading[2], int)
                    and heading[2] > 0
                    for heading in document.headings
                )
                or not isinstance(document.computation_fences, int)
                or document.computation_fences < 0
            ):
                raise BundleError(
                    "snapshot link collections have invalid shapes", code="invalid_snapshot"
                )
            total += len(document.raw.encode("utf-8"))
            if total > limits.max_total_bytes:
                raise BundleError("snapshot byte limit exceeded", code="resource_limit")
            if (
                len(document.links)
                + len(document.wikilinks)
                + len(document.footnote_refs)
                + len(document.footnote_defs)
                > limits.max_findings
            ):
                raise BundleError("snapshot link limit exceeded", code="resource_limit")
            seen.add(document.path)
        return path
    root = checked_root(path)
    documents = []
    total = 0
    for candidate in markdown_paths(root, limits):
        document = _parse_document(candidate, root, limits)
        total += len(document.raw.encode("utf-8"))
        if total > limits.max_total_bytes:
            raise BundleError("bundle byte limit exceeded", code="resource_limit", path=root)
        documents.append(document)
    return BundleSnapshot(root, documents)


def iter_documents(
    bundle_root: Path | BundleSnapshot,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> Iterator[Document]:
    yield from load_bundle(bundle_root, limits=limits).documents


def concepts(
    bundle_root: Path | BundleSnapshot,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> list[Document]:
    return [d for d in iter_documents(bundle_root, limits=limits) if not d.is_reserved]


def reserved(
    bundle_root: Path | BundleSnapshot,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> list[Document]:
    return [d for d in iter_documents(bundle_root, limits=limits) if d.is_reserved]
