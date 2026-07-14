"""Hygiene checks for OKF bundles: the failures conformance does not catch.

Why a linter has to exist
------------------------
OKF is permissive by design. The spec instructs consumers:

    "Consumers MUST NOT reject a bundle" for missing optional fields, unknown types,
    unrecognized keys, broken links, or missing index files.

That is a good rule for interoperability and a dangerous one for authors, because it means
**a broken bundle is accepted in silence**. Ship a directory whose relationships are all
expressed as Obsidian `[[wikilinks]]` and a conforming consumer will ingest it, report no
error, and see a knowledge graph with zero edges. Nothing tells you the graph vanished.

Every rule below corresponds to a failure that is invisible to `okf validate`, invisible to
a conforming consumer, and therefore invisible to you.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .parser import iter_documents

RECOMMENDED = ("title", "description", "resource", "tags", "timestamp")

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

_STOP = {
    "a", "an", "the", "of", "for", "from", "in", "on", "at", "to", "and", "or",
    "with", "by", "is", "are", "was", "were", "as", "its", "this", "that",
}


@dataclass
class Finding:
    rule: str
    severity: str
    path: str
    message: str
    line: int | None = None


def _concept_key(resource: str, title: str) -> str:
    toks = re.findall(r"[a-z0-9]+", (title or "").lower())
    sig = sorted(t for t in toks if t not in _STOP)
    return hashlib.sha1((str(resource) + "|" + " ".join(sig)).encode()).hexdigest()[:16]


def lint(bundle_root: Path) -> list[Finding]:
    bundle_root = Path(bundle_root).resolve()
    out: list[Finding] = []
    docs = list(iter_documents(bundle_root))
    cs = [d for d in docs if not d.is_reserved]

    inbound: dict[str, int] = defaultdict(int)
    by_key: dict[str, list[str]] = defaultdict(list)

    # Everything a wikilink could resolve to: filename stem, title, or alias.
    slugs: set[str] = set()
    for d in cs:
        slugs.add(d.path.stem.lower())
        if d.frontmatter:
            title = d.frontmatter.get("title")
            if isinstance(title, str):
                slugs.add(title.strip().lower())
            for alias in d.frontmatter.get("aliases") or []:
                slugs.add(str(alias).strip().lower())

    for d in docs:
        # --- broken links -------------------------------------------------------
        # The spec tells consumers not to reject on these, so they fail silently forever.
        for link in d.links:
            target = d.resolve(link)
            if target is None:
                continue
            if not target.exists():
                out.append(Finding(
                    "broken-link", "error", d.rel,
                    f"link target does not exist: {link.href}", link.line,
                ))
            else:
                try:
                    inbound[target.relative_to(bundle_root).as_posix()] += 1
                except ValueError:
                    out.append(Finding(
                        "link-escapes-bundle", "error", d.rel,
                        f"link resolves outside the bundle root: {link.href}", link.line,
                    ))

        # --- wikilinks ----------------------------------------------------------
        # Not in the spec. A consumer sees literal text, so the relationship is lost.
        # Split by whether the target exists, because the two need different actions and
        # telling someone to run `migrate` on a link migrate cannot resolve is useless.
        resolvable = [w for w in d.wikilinks if w.strip().lower() in slugs]
        dangling = [w for w in d.wikilinks if w.strip().lower() not in slugs]
        if resolvable:
            out.append(Finding(
                "wikilink", "error", d.rel,
                f"{len(resolvable)} Obsidian [[wikilink]](s) point at pages that exist but "
                f"are invisible to OKF consumers; these relationships are silently dropped. "
                f"Run `okf migrate --apply`.",
            ))
        if dangling:
            out.append(Finding(
                "wanted-page", "info", d.rel,
                f"{len(dangling)} [[wikilink]](s) reference concepts that have no page "
                f"({', '.join(sorted(set(dangling))[:3])}"
                f"{'...' if len(set(dangling)) > 3 else ''}). "
                f"Not convertible: a link to a page that does not exist would be a dead "
                f"link, and OKF consumers tolerate those in silence. Write the page, or "
                f"leave it as a wanted-page marker.",
            ))

        # --- nested links -------------------------------------------------------
        if re.search(r"\]\([^)]*\[[^\]]*\]\(", d.body):
            out.append(Finding(
                "nested-link", "error", d.rel,
                "a markdown link appears nested inside another link's URL "
                "(usually an auto-linker that re-linked text inside an existing link)",
            ))

        if d.is_reserved:
            continue

        # --- conformance mirrors (also reported by validate) ---------------------
        if d.frontmatter is None:
            out.append(Finding("unparseable-frontmatter", "error", d.rel,
                               d.fm_error or "no YAML frontmatter"))
            continue
        if d.type is None:
            out.append(Finding("missing-type", "error", d.rel,
                               "`type` is the only REQUIRED field and it is missing or empty"))

        # --- the naive-parser trap ----------------------------------------------
        # A frontmatter VALUE containing `---` is legal YAML and legal OKF, but any
        # consumer that splits on the substring `---` instead of on a line equal to
        # `---` will cut the document in half and call it malformed.
        for k, v in d.frontmatter.items():
            if isinstance(v, str) and "---" in v:
                out.append(Finding(
                    "frontmatter-delimiter-in-value", "warning", d.rel,
                    f"field `{k}` contains `---`; consumers that split on the SUBSTRING "
                    f"`---` rather than a LINE equal to `---` will misparse this document",
                ))

        # --- recommended fields --------------------------------------------------
        missing = [f for f in RECOMMENDED if not d.frontmatter.get(f)]
        if missing:
            out.append(Finding(
                "missing-recommended", "info", d.rel,
                f"missing recommended field(s): {', '.join(missing)}",
            ))

        # --- duplicate concepts ---------------------------------------------------
        # A duplicate page is not a broken link, so nothing reports it. Producers whose
        # page identity derives from a model-written title fork the bundle on every
        # re-ingest, because the model rewords the title.
        res = str(d.frontmatter.get("resource") or "")
        title = str(d.frontmatter.get("title") or d.path.stem)
        if res:
            by_key[_concept_key(res, title)].append(d.rel)

    for key, paths in by_key.items():
        if len(paths) > 1:
            out.append(Finding(
                "duplicate-concept", "warning", paths[0],
                "same `resource` and same significant-title word bag as: "
                + ", ".join(paths[1:])
                + " (a duplicate page is not a broken link, so nothing else reports it)",
            ))

    # --- orphans -----------------------------------------------------------------
    for d in cs:
        if inbound.get(d.rel, 0) == 0:
            out.append(Finding("orphan", "info", d.rel, "no inbound links from the bundle"))

    out.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.path, f.line or 0))
    return out
