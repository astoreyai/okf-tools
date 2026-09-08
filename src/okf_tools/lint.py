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

v0.2 widened this hazard rather than narrowing it. The conformance criteria (§11) are
unchanged from v0.1 §9, while the format gained provenance, trust, lifecycle, and attestation
vocabulary — all of it optional, none of it checked by conformance. A concept whose
`attester.resource` points at a file that does not exist is fully conformant, and the
attestation chain it advertises is severed.

Every rule below corresponds to a failure that is invisible to `okf validate`, invisible to
a conforming consumer, and therefore invisible to you.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

from .parser import (
    BundleSnapshot,
    _index_targets,
    _resolve_target,
    is_conventional_actor,
    load_bundle,
    normalize_verified,
)
from .safety import DEFAULT_LIMITS, BundleError, Limits

# §4.1 recommended fields. `timestamp` is NOT among them: v0.2 §13.1 supersedes it with
# `generated.at`. Trust and provenance are reported separately, by `missing-provenance`.
RECOMMENDED = ("title", "description", "resource", "tags")

STATUS_VALUES = {"draft", "stable", "deprecated"}

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_STOP = {
    "a",
    "an",
    "the",
    "of",
    "for",
    "from",
    "in",
    "on",
    "at",
    "to",
    "and",
    "or",
    "with",
    "by",
    "is",
    "are",
    "was",
    "were",
    "as",
    "its",
    "this",
    "that",
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


def _as_instant(value) -> datetime | None:
    """Normalize aware ISO instants and legacy dates (UTC midnight), never naive times."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    elif isinstance(value, str):
        value = value.strip()
        try:
            if ISO_DATE.fullmatch(value):
                return datetime.combine(date.fromisoformat(value), time.min, tzinfo=timezone.utc)
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


class _Findings(list):
    def __init__(self, limits: Limits) -> None:
        super().__init__()
        self.maximum = limits.max_findings

    def append(self, finding: Finding) -> None:
        if len(self) >= self.maximum:
            raise BundleError("diagnostic limit exceeded", code="resource_limit", path=finding.path)
        super().append(finding)


def lint(
    bundle_root: Path | BundleSnapshot,
    today: date | datetime | None = None,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> list[Finding]:
    snapshot = load_bundle(bundle_root, limits=limits)
    bundle_root = snapshot.root
    now = datetime.now(timezone.utc) if today is None else _as_instant(today)
    if now is None:
        raise BundleError("today must be a date or a timezone-aware datetime", code="invalid_time")
    out: list[Finding] = _Findings(limits)
    docs = snapshot.documents
    cs = [d for d in docs if not d.is_reserved]

    inbound: dict[str, int] = defaultdict(int)
    by_key: dict[str, list[tuple[str, bool]]] = defaultdict(list)

    targets = _index_targets(cs)
    paths = {doc.rel for doc in cs}
    folded_paths: dict[str, set[str]] = {}
    for path in paths:
        folded_paths.setdefault(path.casefold(), set()).add(path)

    for d in docs:
        # --- broken links -------------------------------------------------------
        # The spec tells consumers not to reject on these, so they fail silently forever.
        for link in d.links:
            try:
                target = d.resolve(link)
            except BundleError as exc:
                out.append(Finding("invalid-link-path", "error", d.rel, str(exc), link.line))
                continue
            if target is None:
                continue
            try:
                relative = target.relative_to(bundle_root).as_posix()
            except ValueError:
                out.append(
                    Finding(
                        "link-escapes-bundle",
                        "error",
                        d.rel,
                        f"link resolves outside the bundle root: {link.href}",
                        link.line,
                    )
                )
                continue
            if not target.exists():
                out.append(
                    Finding(
                        "broken-link",
                        "error",
                        d.rel,
                        f"link target does not exist: {link.href}",
                        link.line,
                    )
                )
            else:
                inbound[relative] += 1

        # --- wikilinks ----------------------------------------------------------
        # Not in the spec. A consumer sees literal text, so the relationship is lost.
        # Split by whether the target exists, because the two need different actions and
        # telling someone to run `migrate` on a link migrate cannot resolve is useless.
        resolvable, dangling, ambiguous = [], [], []
        for target in d.wikilinks:
            candidates, _ = _resolve_target(target, d, paths, folded_paths, targets)
            if len(candidates) == 1:
                resolvable.append(target)
            elif candidates:
                ambiguous.append(target)
            else:
                dangling.append(target)
        if ambiguous:
            out.append(
                Finding(
                    "ambiguous-wikilink",
                    "error",
                    d.rel,
                    f"{len(ambiguous)} [[wikilink]](s) match multiple concepts; "
                    "use an explicit relative or bundle-root path before migrating.",
                )
            )
        if resolvable:
            out.append(
                Finding(
                    "wikilink",
                    "error",
                    d.rel,
                    f"{len(resolvable)} Obsidian [[wikilink]](s) point at pages that exist but "
                    f"are invisible to OKF consumers; these relationships are silently dropped. "
                    f"Run `okf migrate --apply`.",
                )
            )
        if dangling:
            out.append(
                Finding(
                    "wanted-page",
                    "info",
                    d.rel,
                    f"{len(dangling)} [[wikilink]](s) reference concepts that have no page "
                    f"({', '.join(sorted(set(dangling))[:3])}"
                    f"{'...' if len(set(dangling)) > 3 else ''}). "
                    f"Not convertible: a link to a page that does not exist would be a dead "
                    f"link, and OKF consumers tolerate those in silence. Write the page, or "
                    f"leave it as a wanted-page marker.",
                )
            )

        if d.is_reserved:
            continue

        # --- conformance mirrors (also reported by validate) ---------------------
        if d.frontmatter is None:
            out.append(
                Finding(
                    "unparseable-frontmatter", "error", d.rel, d.fm_error or "no YAML frontmatter"
                )
            )
            continue
        if d.type is None:
            out.append(
                Finding(
                    "missing-type",
                    "error",
                    d.rel,
                    "`type` is the only REQUIRED field and it is missing or empty",
                )
            )

        # --- the naive-parser trap ----------------------------------------------
        # A frontmatter VALUE containing `---` is legal YAML and legal OKF, but any
        # consumer that splits on the substring `---` instead of on a line equal to
        # `---` will cut the document in half and call it malformed.
        for k, v in d.frontmatter.items():
            if isinstance(v, str) and "---" in v:
                out.append(
                    Finding(
                        "frontmatter-delimiter-in-value",
                        "warning",
                        d.rel,
                        f"field `{k}` contains `---`; consumers that split on the SUBSTRING "
                        f"`---` rather than a LINE equal to `---` will misparse this document",
                    )
                )

        _lint_shapes(d, out)
        _lint_paths(d, bundle_root, inbound, out)
        _lint_provenance(d, out)
        _lint_trust(d, out)
        _lint_lifecycle(d, now, out)
        _lint_computation(d, out)

        # --- recommended fields --------------------------------------------------
        missing = [f for f in RECOMMENDED if not d.frontmatter.get(f)]
        if missing:
            out.append(
                Finding(
                    "missing-recommended",
                    "info",
                    d.rel,
                    f"missing recommended field(s): {', '.join(missing)}",
                )
            )

        # --- duplicate concepts ---------------------------------------------------
        # A duplicate page is not a broken link, so nothing reports it. Producers whose
        # page identity derives from a model-written title fork the bundle on every
        # re-ingest, because the model rewords the title.
        res = d.frontmatter.get("resource")
        title = d.frontmatter.get("title")
        title = title if isinstance(title, str) and title.strip() else d.path.stem
        if isinstance(res, str) and res.strip():
            deprecated = d.frontmatter.get("status") == "deprecated"
            by_key[_concept_key(res, title)].append((d.rel, deprecated))

    for key, entries in by_key.items():
        # §5.4 makes `deprecated` the sanctioned way to retain a superseded concept beside
        # its replacement. That pair is intentional, not a fork, so only live concepts count.
        live = [rel for rel, deprecated in entries if not deprecated]
        if len(live) > 1:
            out.append(
                Finding(
                    "duplicate-concept",
                    "warning",
                    live[0],
                    "same `resource` and same significant-title word bag as: "
                    + ", ".join(live[1:])
                    + " (a duplicate page is not a broken link, so nothing else reports it)",
                )
            )

    # --- orphans -----------------------------------------------------------------
    for d in cs:
        if inbound.get(d.rel, 0) == 0:
            out.append(Finding("orphan", "info", d.rel, "no inbound links from the bundle"))

    out.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.path, f.line or 0))
    return out


def _lint_paths(d, bundle_root: Path, inbound: dict[str, int], out: list[Finding]) -> None:
    """§6.2 path-valued frontmatter fields are links that nothing else checks.

    `attester.resource`, `executor.resource`, `computation`, and `sources[].resource` can all
    point inside the bundle. A broken one severs the attestation chain or the provenance
    trail exactly the way a broken markdown link severs a relationship — and because these
    are frontmatter rather than body links, no link checker has ever looked at them.
    """
    for pf in d.path_fields():
        try:
            target = d.resolve_path_field(pf)
        except BundleError as exc:
            rule = (
                "ambiguous-frontmatter-path"
                if exc.code == "ambiguous_path"
                else "invalid-frontmatter-path"
            )
            out.append(Finding(rule, "error", d.rel, str(exc)))
            continue
        if target is None:
            continue
        try:
            relative = target.relative_to(bundle_root).as_posix()
        except ValueError:
            out.append(
                Finding(
                    "frontmatter-path-escapes-bundle",
                    "error",
                    d.rel,
                    f"`{pf.label}` resolves outside the bundle root: {pf.value}",
                )
            )
            continue
        if not target.exists():
            out.append(
                Finding(
                    "broken-frontmatter-path",
                    "error",
                    d.rel,
                    f"`{pf.label}` points at a path that does not exist: {pf.value}",
                )
            )
        else:
            inbound[relative] += 1


def _lint_shapes(d, out: list[Finding]) -> None:
    """Validate only named metadata contracts; unknown extension fields stay opaque."""
    fm = d.frontmatter or {}

    def invalid(label: str, expected: str, severity: str = "error") -> None:
        out.append(
            Finding("invalid-metadata-shape", severity, d.rel, f"`{label}` must be {expected}")
        )

    def string(mapping: dict, key: str, label: str) -> None:
        if key in mapping and (not isinstance(mapping[key], str) or not mapping[key].strip()):
            invalid(label, "a non-empty string")

    def timestamp(mapping: dict, key: str, label: str) -> None:
        if key in mapping and _as_instant(mapping[key]) is None:
            out.append(
                Finding(
                    "invalid-timestamp",
                    "error",
                    d.rel,
                    f"`{label}` must be an ISO 8601 datetime with an explicit "
                    "UTC offset (legacy dates are interpreted at UTC midnight)",
                )
            )

    def window(mapping: dict, label: str) -> None:
        if "usage_window" not in mapping:
            return
        value = mapping["usage_window"]
        if not isinstance(value, dict):
            invalid(label, "a mapping with `from` and `to` timestamps")
            return
        for key in ("from", "to"):
            if key not in value:
                invalid(f"{label}.{key}", "a timestamp")
            else:
                timestamp(value, key, f"{label}.{key}")
        start, end = _as_instant(value.get("from")), _as_instant(value.get("to"))
        if start is not None and end is not None and start > end:
            out.append(
                Finding(
                    "invalid-usage-window", "error", d.rel, f"`{label}.from` is after `{label}.to`"
                )
            )

    for key in ("title", "description", "resource", "status", "runtime", "computation"):
        string(fm, key, key)
    # Tags are optional discovery metadata; malformed tags are advisory, unlike
    # aliases whose shape participates in relationship resolution.
    for key, severity in (("aliases", "error"), ("tags", "warning")):
        if key not in fm:
            continue
        value = fm[key]
        if not isinstance(value, list):
            invalid(key, "a list of non-empty strings", severity)
        else:
            for index, entry in enumerate(value):
                if not isinstance(entry, str) or not entry.strip():
                    invalid(f"{key}[{index}]", "a non-empty string", severity)
    timestamp(fm, "timestamp", "timestamp")
    window(fm, "usage_window")

    for key in ("generated", "verified"):
        if key not in fm:
            continue
        value = fm[key]
        if isinstance(value, dict):
            entries = [value]
        elif key == "verified" and isinstance(value, list):
            entries = value
        else:
            invalid(key, "a mapping" if key == "generated" else "a mapping or list of mappings")
            continue
        for index, entry in enumerate(entries):
            label = key if key == "generated" else f"{key}[{index}]"
            if not isinstance(entry, dict):
                invalid(label, "a mapping containing `by` and `at`")
                continue
            string(entry, "by", f"{label}.by")
            timestamp(entry, "at", f"{label}.at")
            if key == "verified" and "at" not in entry:
                invalid(f"{label}.at", "a timestamp")

    if "sources" in fm:
        sources = fm["sources"]
        if not isinstance(sources, list):
            invalid("sources", "a list of source mappings")
            sources = [sources] if isinstance(sources, dict) else []
        for index, entry in enumerate(sources):
            label = f"sources[{index}]"
            if not isinstance(entry, dict):
                invalid(label, "a mapping with a non-empty `resource`")
                continue
            for key in ("resource", "id", "title", "author"):
                string(entry, key, f"{label}.{key}")
            timestamp(entry, "last_modified", f"{label}.last_modified")
            window(entry, f"{label}.usage_window")
            if "usage_count" in entry:
                count = entry["usage_count"]
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    invalid(f"{label}.usage_count", "a non-negative integer")

    for key in ("executor", "attester"):
        if key not in fm:
            continue
        value = fm[key]
        if not isinstance(value, dict):
            invalid(key, "a mapping with a non-empty `resource`")
        else:
            string(value, "resource", f"{key}.resource")
            if "resource" not in value:
                invalid(f"{key}.resource", "a non-empty string")


def _lint_provenance(d, out: list[Finding]) -> None:
    """§5.1 sources, their credibility signals, and footnote attribution."""
    fm = d.frontmatter or {}
    sources = d.sources
    source_ids = {
        entry["id"].strip()
        for entry in sources
        if isinstance(entry.get("id"), str) and entry["id"].strip()
    }

    for i, entry in enumerate(sources):
        resource = entry.get("resource")
        if not isinstance(resource, str) or not resource.strip():
            out.append(
                Finding(
                    "incomplete-source",
                    "error",
                    d.rel,
                    f"`sources[{i}]` has no `resource`; it is REQUIRED within an entry (§5.1)",
                )
            )
        author = entry.get("author")
        if isinstance(author, str) and author.strip() and not is_conventional_actor(author):
            out.append(
                Finding(
                    "nonconventional-actor",
                    "warning",
                    d.rel,
                    f"`sources[{i}].author` is `{author}`, which matches no §7 actor form "
                    f"(`human:<id>`, `process:<id>`, `team:<id>`, or `<producer>/<version>`)",
                )
            )
        # §5.1: usage_window frames every usage_count. Without one the number has no period
        # attached, so it cannot be read as the adoption signal it is meant to be.
        if entry.get("usage_count") is not None:
            if not entry.get("usage_window") and not fm.get("usage_window"):
                out.append(
                    Finding(
                        "unframed-usage-count",
                        "info",
                        d.rel,
                        f"`sources[{i}].usage_count` has no `usage_window` on the entry or as a "
                        f"sibling of `sources`; the count has no period attached (§5.1)",
                    )
                )

    # A footnote reference with neither a definition nor a matching `sources[].id` resolves
    # to nothing at all. That is unambiguous.
    #
    # Deliberately NOT flagged: a footnote that has a definition but does not match a source
    # id. The reference bundles use plain `[^1]` footnotes for ordinary asides alongside
    # id-keyed ones, so a label-not-in-sources rule would fail the spec authors' own work.
    # It also means a RENAMED source id cannot be distinguished from an ordinary footnote;
    # `uncited-source` below is the signal that catches that case from the other side.
    for label, line in d.footnote_refs:
        if label in d.footnote_defs or label in source_ids:
            continue
        out.append(
            Finding(
                "dangling-footnote",
                "error",
                d.rel,
                f"footnote `[^{label}]` has no definition in the body and matches no "
                f"`sources[].id`; the attribution resolves to nothing (§5.1)",
                line,
            )
        )

    # Only meaningful in a document that actually uses footnote attribution. A source `id`
    # in a document with no source-keyed footnotes is not a dropped citation — the source
    # simply backs the document as a whole, which is the common case throughout the
    # reference bundles. But once a document cites SOME of its sources by footnote, one that
    # is declared and never cited is the shape a renamed or reordered id leaves behind.
    cited = {label for label, _ in d.footnote_refs}
    if cited & source_ids:
        for sid in sorted(source_ids - cited):
            out.append(
                Finding(
                    "uncited-source",
                    "info",
                    d.rel,
                    f"`sources` entry `{sid}` carries an `id`, and this document cites other "
                    f"sources by footnote, but nothing cites `[^{sid}]`; a renamed or reordered "
                    f"`id` leaves exactly this trace (§5.1)",
                )
            )


def _lint_trust(d, out: list[Finding]) -> None:
    """§5.2 generated/verified and the §7 actor convention that trust tiers key off."""
    fm = d.frontmatter or {}

    generated = fm.get("generated")
    if generated is not None:
        if not isinstance(generated, dict):
            out.append(
                Finding(
                    "incomplete-generated",
                    "error",
                    d.rel,
                    "`generated` must be a mapping of `{ by, at }` (§5.2)",
                )
            )
        else:
            by = generated.get("by")
            if not isinstance(by, str) or not by.strip():
                out.append(
                    Finding(
                        "incomplete-generated",
                        "error",
                        d.rel,
                        "`generated` has no `by`; it is REQUIRED within `generated` (§5.2)",
                    )
                )
            elif not is_conventional_actor(by):
                out.append(
                    Finding(
                        "nonconventional-actor",
                        "warning",
                        d.rel,
                        f"`generated.by` is `{by}`, which matches no §7 actor form",
                    )
                )
    elif fm.get("timestamp"):
        out.append(
            Finding(
                "legacy-timestamp",
                "info",
                d.rel,
                "`timestamp` is a v0.1 field superseded by `generated.at` (§13.1); consumers "
                "MAY fall back to it, but `generated: { by, at }` is the v0.2 form",
            )
        )
    elif d.type:
        out.append(
            Finding(
                "missing-provenance",
                "info",
                d.rel,
                "no `generated`; a consumer cannot tell who or what produced this concept, "
                "which is the question v0.2 trust signals exist to answer (§5.2)",
            )
        )

    for i, event in enumerate(normalize_verified(fm)):
        by = event.get("by")
        if not isinstance(by, str) or not by.strip():
            out.append(
                Finding(
                    "incomplete-verified",
                    "error",
                    d.rel,
                    f"`verified[{i}]` has no `by`; a verification event needs an actor (§5.2)",
                )
            )
        elif not is_conventional_actor(by):
            out.append(
                Finding(
                    "nonconventional-actor",
                    "warning",
                    d.rel,
                    f"`verified[{i}].by` is `{by}`, which matches no §7 actor form. Trust tiers "
                    f"key off the `human:` prefix (§5.3), so a human sign-off written without it "
                    f"is silently downgraded to machine-confirmed",
                )
            )


def _lint_lifecycle(d, today: datetime, out: list[Finding]) -> None:
    """§5.4 status and §5.5 stale_after."""
    fm = d.frontmatter or {}

    status = fm.get("status")
    if isinstance(status, str) and status.strip() not in STATUS_VALUES:
        out.append(
            Finding(
                "unknown-status",
                "warning",
                d.rel,
                f"`status` is `{status}`; §5.4 defines draft, stable, deprecated "
                f"(absent means stable)",
            )
        )

    if "stale_after" not in fm:
        return
    when = _as_instant(fm.get("stale_after"))
    if when is None:
        out.append(
            Finding(
                "invalid-stale-after",
                "error",
                d.rel,
                "`stale_after` requires an absolute ISO 8601 datetime with an explicit "
                "UTC offset, or a legacy `YYYY-MM-DD` date, not a naive time or relative TTL",
            )
        )
    elif today >= when:
        out.append(
            Finding(
                "stale",
                "warning",
                d.rel,
                f"past its `stale_after` of {when.isoformat()} as of {today.isoformat()}; "
                f"§10.5 says a consumer SHOULD warn or refuse (§5.5)",
            )
        )


def _lint_computation(d, out: list[Finding]) -> None:
    """§10 Attested Computation contract fields."""
    if d.type != "Attested Computation":
        return
    fm = d.frontmatter or {}

    runtime = fm.get("runtime")
    if not isinstance(runtime, str) or not runtime.strip():
        out.append(
            Finding(
                "missing-runtime",
                "error",
                d.rel,
                "`runtime` is REQUIRED for `type: Attested Computation` (§10.2); it is what "
                "defines how the executor and attester read the computation and what "
                "`parameters` mean",
            )
        )

    for key in ("executor", "attester"):
        block = fm.get(key)
        if block is None:
            continue
        if (
            not isinstance(block, dict)
            or not isinstance(block.get("resource"), str)
            or not block["resource"].strip()
        ):
            out.append(
                Finding(
                    f"incomplete-{key}",
                    "error",
                    d.rel,
                    f"`{key}` carries no `resource`, so there is nothing to "
                    f"{'run' if key == 'executor' else 'check the receipt with'} (§10.2)",
                )
            )

    # §10.3: the computation is either an inline `# Computation` fence or a `computation`
    # path, and setting the path means omitting the fence.
    computation = fm.get("computation")
    has_path = isinstance(computation, str) and bool(computation.strip())
    has_section = any(
        level == 1 and title.strip() == "Computation" for level, title, _ in d.headings
    )
    if has_path and has_section:
        out.append(
            Finding(
                "ambiguous-computation",
                "warning",
                d.rel,
                "both a `computation` path and a body `# Computation` section are present; "
                "§10.3 says to set the path and omit the fence, so a consumer cannot tell "
                "which one the attester will compare against",
            )
        )
    elif not has_path and not has_section:
        out.append(
            Finding(
                "missing-computation",
                "error",
                d.rel,
                "an Attested Computation carries neither a `computation` path nor a body "
                "`# Computation` section, so there is no sanctioned computation to attest (§10.3)",
            )
        )
    elif not has_path and has_section and not d.computation_fences:
        out.append(
            Finding(
                "missing-computation-body",
                "error",
                d.rel,
                "the `# Computation` section has no non-empty fenced code block (§10.3)",
            )
        )
    if d.computation_fences > 1:
        out.append(
            Finding(
                "multiple-computation-bodies",
                "error",
                d.rel,
                "the `# Computation` section must contain a single fenced code block (§10.3)",
            )
        )
