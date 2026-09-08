"""OKF v0.2 section 11 conformance.

The spec asks for exactly three things of a bundle:

  1. every non-reserved .md file contains parseable YAML frontmatter
  2. every frontmatter block contains a non-empty `type` field
  3. reserved filenames follow their specified structures when present

That is the whole conformance surface, and it is unchanged from v0.1 §9 — v0.2 added a large
optional vocabulary (provenance, trust, lifecycle, attestation) without adding a single
conformance requirement. `type` remains the only REQUIRED field of a concept.

Conformance is therefore a low bar on purpose, and passing it says much less about a bundle
than people assume. Everything v0.2 added is checked by `lint`, not here: a bundle whose
attester points at a file that does not exist is fully conformant. See `lint` for what
conformance does not catch.

Criterion 3 enforces the MUSTs of §8 and §9 and nothing more:

  - `index.md` carries no frontmatter, except that a bundle-root `index.md` MAY carry an
    `okf_version` key (§8, §12). A bundle that declares the version it targets is valid, and
    an earlier version of this checker rejected it.
  - `log.md` date headings MUST use ISO 8601 `YYYY-MM-DD` form (§9). Nothing in §9 forbids
    frontmatter on a `log.md`; the reference bundle `acme_retail/log.md` carries `type: Log`.
    Applying the index rule to logs was a bug that failed the spec authors' own bundle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .parser import BundleSnapshot, load_bundle
from .safety import DEFAULT_LIMITS, BundleError, Limits

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Failure:
    criterion: int
    path: str
    message: str


@dataclass
class Report:
    concepts: int
    reserved: int
    failures: list[Failure]

    @property
    def conformant(self) -> bool:
        return not self.failures


def _check_index(doc, failures: _Failures) -> None:
    """§8: index files carry no frontmatter; the bundle-root index may declare okf_version."""
    if doc.frontmatter is None and doc.fm_error is None:
        return
    fm, err = doc.frontmatter, doc.fm_error
    is_root = doc.path.parent == doc.bundle_root
    if not is_root:
        failures.append(
            Failure(
                3,
                doc.rel,
                "`index.md` must not carry frontmatter (§8); only a bundle-root "
                "`index.md` may, and then only to declare `okf_version`",
            )
        )
        return
    if err or fm is None:
        failures.append(Failure(3, doc.rel, err or "unparseable frontmatter"))
        return
    extra = sorted(str(k) for k in fm if k != "okf_version")
    if extra:
        failures.append(
            Failure(
                3,
                doc.rel,
                f"a bundle-root `index.md` may carry only `okf_version` in frontmatter "
                f"(§8, §12); found: {', '.join(extra)}",
            )
        )


def _check_log(doc, failures: _Failures) -> None:
    """§9: date headings MUST use ISO 8601 YYYY-MM-DD. Frontmatter is not prohibited."""
    for level, heading, _ in doc.headings:
        if level != 2:
            continue
        heading = heading.strip()
        valid = bool(ISO_DATE.fullmatch(heading))
        if valid:
            try:
                date.fromisoformat(heading)
            except ValueError:
                valid = False
        if not valid:
            failures.append(
                Failure(
                    3,
                    doc.rel,
                    f"log date heading `## {heading}` is not ISO 8601 `YYYY-MM-DD` (§9)",
                )
            )


class _Failures:
    def __init__(self, limits: Limits) -> None:
        self.items: list[Failure] = []
        self.maximum = limits.max_findings

    def append(self, failure: Failure) -> None:
        if len(self.items) >= self.maximum:
            raise BundleError("diagnostic limit exceeded", code="resource_limit", path=failure.path)
        self.items.append(failure)


def validate(
    bundle_root: Path | BundleSnapshot,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> Report:
    snapshot = load_bundle(bundle_root, limits=limits)
    failures = _Failures(limits)
    concept_count = reserved_count = 0
    for d in snapshot.documents:
        if d.is_reserved:
            reserved_count += 1
            if d.path.name == "index.md":
                _check_index(d, failures)
            elif d.path.name == "log.md":
                _check_log(d, failures)
            continue
        concept_count += 1
        if d.frontmatter is None:
            failures.append(Failure(1, d.rel, d.fm_error or "no YAML frontmatter"))
        elif d.type is None:
            failures.append(Failure(2, d.rel, "missing or empty `type` field"))
    return Report(concepts=concept_count, reserved=reserved_count, failures=failures.items)
