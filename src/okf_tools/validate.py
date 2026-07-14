"""OKF v0.1 section 9 conformance.

The spec asks for exactly three things of a bundle:

  1. every non-reserved .md file contains parseable YAML frontmatter
  2. every frontmatter block contains a non-empty `type` field
  3. reserved filenames follow their specified structures when present

That is the whole conformance surface. `type` is the only REQUIRED field of a concept;
everything else (which types exist, which other fields appear, what the body contains) is
left to the producer. Conformance is therefore a low bar on purpose, and passing it says
much less about a bundle than people assume. See `lint` for what conformance does not catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .parser import concepts, reserved


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


def validate(bundle_root: Path) -> Report:
    failures: list[Failure] = []
    cs = concepts(bundle_root)
    rs = reserved(bundle_root)

    for d in cs:
        # Criterion 1: parseable YAML frontmatter.
        if d.frontmatter is None:
            failures.append(
                Failure(1, d.rel, d.fm_error or "no YAML frontmatter")
            )
            continue
        # Criterion 2: non-empty `type`.
        if d.type is None:
            failures.append(Failure(2, d.rel, "missing or empty `type` field"))

    # Criterion 3: reserved files follow their structure.
    # The spec is explicit that index files contain NO frontmatter, and that reserved
    # names MUST NOT be used for concept documents.
    for d in rs:
        if d.raw.startswith("---"):
            failures.append(
                Failure(
                    3, d.rel,
                    f"reserved file `{d.path.name}` must not carry frontmatter "
                    "(it is not a concept document)",
                )
            )

    return Report(concepts=len(cs), reserved=len(rs), failures=failures)
