"""Tests for okf-tools.

The fixtures are the OKF project's own reference bundles, vendored from
github.com/GoogleCloudPlatform/knowledge-catalog (Apache-2.0). Testing a format tool
against bundles authored by the people who wrote the format is the only test that means
anything: if this tool disagrees with them, this tool is wrong.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from okf_tools import build_indexes, lint, migrate, validate
from okf_tools.parser import split_frontmatter

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_BUNDLES = ["ga4", "crypto_bitcoin", "stackoverflow"]


# --------------------------------------------------------------------------- parser


def test_frontmatter_ends_at_a_line_not_a_substring():
    """A value containing `---` must not truncate the frontmatter.

    This is the trap: `id: topic-etl---nightly-loader` is a legal document, and a parser
    that splits on the SUBSTRING `---` cuts it in half and calls it malformed.
    """
    text = (
        "---\n"
        "type: topic\n"
        "id: topic-etl---nightly-loader\n"
        "title: etl - Nightly Loader\n"
        "---\n"
        "\n# Body\n"
    )
    fm, body, err = split_frontmatter(text)
    assert err is None
    assert fm is not None
    assert fm["type"] == "topic"
    assert fm["id"] == "topic-etl---nightly-loader"
    assert "# Body" in body


def test_unclosed_frontmatter_is_an_error():
    fm, _, err = split_frontmatter("---\ntype: topic\nnever closed\n")
    assert fm is None
    assert err and "never closed" in err


def test_no_frontmatter_is_not_an_error():
    fm, body, err = split_frontmatter("# Just a heading\n")
    assert fm is None and err is None and body.startswith("# Just")


def test_malformed_yaml_reports_an_error():
    fm, _, err = split_frontmatter('---\ntype: "unterminated\n---\nbody\n')
    assert fm is None
    assert err and "unparseable YAML" in err


# ----------------------------------------------------------------------- validate


@pytest.mark.parametrize("bundle", REFERENCE_BUNDLES)
def test_reference_bundles_are_conformant(bundle):
    """The spec authors' own bundles must pass. If they do not, our validator is wrong."""
    report = validate(FIXTURES / bundle)
    assert report.conformant, [f"{f.path}: {f.message}" for f in report.failures]
    assert report.concepts > 0


def test_missing_type_fails_criterion_2(tmp_path):
    (tmp_path / "a.md").write_text("---\ntitle: No type here\n---\n\n# A\n")
    report = validate(tmp_path)
    assert not report.conformant
    assert any(f.criterion == 2 for f in report.failures)


def test_unparseable_frontmatter_fails_criterion_1(tmp_path):
    (tmp_path / "a.md").write_text('---\ntype: "unterminated\n---\n\n# A\n')
    report = validate(tmp_path)
    assert any(f.criterion == 1 for f in report.failures)


def test_reserved_index_with_frontmatter_fails_criterion_3(tmp_path):
    (tmp_path / "index.md").write_text("---\ntype: topic\n---\n\n# Index\n")
    report = validate(tmp_path)
    assert any(f.criterion == 3 for f in report.failures)


# --------------------------------------------------------------------------- lint


@pytest.mark.parametrize("bundle", REFERENCE_BUNDLES)
def test_reference_bundles_have_no_lint_errors(bundle):
    errors = [f for f in lint(FIXTURES / bundle) if f.severity == "error"]
    assert not errors, [f"{f.path}: {f.rule}: {f.message}" for f in errors]


def test_broken_link_is_an_error(tmp_path):
    (tmp_path / "a.md").write_text("---\ntype: topic\n---\n\n[gone](/nope.md)\n")
    rules = {f.rule for f in lint(tmp_path) if f.severity == "error"}
    assert "broken-link" in rules


def test_resolvable_wikilink_is_an_error(tmp_path):
    """A wikilink to a page that EXISTS is a real, silently-dropped relationship."""
    (tmp_path / "other.md").write_text("---\ntype: topic\ntitle: Other Page\n---\n\nbody\n")
    (tmp_path / "a.md").write_text("---\ntype: topic\n---\n\nSee [[Other Page]].\n")
    errors = {f.rule for f in lint(tmp_path) if f.severity == "error"}
    assert "wikilink" in errors


def test_dangling_wikilink_is_a_wanted_page_not_an_error(tmp_path):
    """A wikilink to a page that does not exist cannot be migrated: converting it would
    produce a dead link. It is a wanted page, not an error, and telling the user to run
    `migrate` on it would be useless advice."""
    (tmp_path / "a.md").write_text("---\ntype: topic\n---\n\nSee [[Never Written]].\n")
    findings = lint(tmp_path)
    assert not [f for f in findings if f.rule == "wikilink"]
    wanted = [f for f in findings if f.rule == "wanted-page"]
    assert wanted and wanted[0].severity == "info"


def test_frontmatter_delimiter_in_value_is_flagged(tmp_path):
    (tmp_path / "a.md").write_text("---\ntype: topic\nid: x---y\n---\n\n# A\n")
    rules = {f.rule for f in lint(tmp_path)}
    assert "frontmatter-delimiter-in-value" in rules


def test_duplicate_concept_is_flagged(tmp_path):
    """Same resource, same title word-bag, different filenames: a fork, not a broken link."""
    (tmp_path / "a.md").write_text(
        "---\ntype: topic\nresource: file:///s.md\ntitle: G0 Fix List for Review\n---\n\nx\n"
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: topic\nresource: file:///s.md\ntitle: Review G0 Fix List\n---\n\nx\n"
    )
    rules = {f.rule for f in lint(tmp_path)}
    assert "duplicate-concept" in rules


def test_external_links_are_not_broken(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\ntype: topic\n---\n\n[ok](https://example.com) [anchor](#s)\n"
    )
    assert not [f for f in lint(tmp_path) if f.rule == "broken-link"]


# ------------------------------------------------------------------------ migrate


def test_migrate_converts_wikilinks_and_is_a_dry_run_by_default(tmp_path):
    (tmp_path / "target.md").write_text("---\ntype: topic\ntitle: Target Page\n---\n\nbody\n")
    src = tmp_path / "src.md"
    src.write_text("---\ntype: topic\n---\n\nSee [[Target Page]] and [[target]].\n")

    dry = migrate(tmp_path, apply=False)
    assert dry.links_converted == 2
    assert "[[Target Page]]" in src.read_text(), "dry run must not write"

    applied = migrate(tmp_path, apply=True)
    assert applied.links_converted == 2
    out = src.read_text()
    assert "[Target Page](/target.md)" in out
    assert "[[" not in out


def test_migrate_leaves_unresolvable_wikilinks_alone(tmp_path):
    """Converting an unresolvable wikilink would create a dead link, which OKF tolerates
    in silence. Leaving it is the honest outcome; it is a wanted page, not an error."""
    src = tmp_path / "a.md"
    src.write_text("---\ntype: topic\n---\n\nSee [[Never Written]].\n")
    result = migrate(tmp_path, apply=True)
    assert result.links_converted == 0
    assert "[[Never Written]]" in src.read_text()
    assert "Never Written" in result.wanted_pages


def test_migrate_never_invents_a_description(tmp_path):
    """A document with no prose gets no description. Omission is safe; fabrication is not."""
    src = tmp_path / "a.md"
    src.write_text("---\ntype: topic\n---\n\n```mermaid\nfoo[\"Bar\"]\n```\n")
    migrate(tmp_path, apply=True)
    text = src.read_text()
    assert "description" not in text, "must not lift a description out of a code fence"


# -------------------------------------------------------------------------- index


def test_index_has_no_frontmatter_and_is_conformant(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\ntype: topic\ntitle: Alpha\ndescription: The first one.\n---\n\nbody\n"
    )
    build_indexes(tmp_path, apply=True)
    idx = (tmp_path / "index.md").read_text()
    assert not idx.startswith("---"), "reserved index.md must carry no frontmatter"
    assert "* [Alpha](a.md) - The first one." in idx
    assert validate(tmp_path).conformant


def test_round_trip_reference_bundle_stays_conformant(tmp_path):
    """Regenerating indexes over a real bundle must not break its conformance."""
    dst = tmp_path / "ga4"
    shutil.copytree(FIXTURES / "ga4", dst)
    build_indexes(dst, apply=True)
    assert validate(dst).conformant
    assert not [f for f in lint(dst) if f.severity == "error"]
