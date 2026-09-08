"""Behavioral regressions using published OKF documents, never invented concepts.

Historical bundles are vendored under fixtures/. upstream.json records exact bytes,
SHA-256 hashes and the canonical revision for the current Acme bundle. Negative cases
remove fields/files, alter representation, or rearrange these actual documents; they
do not invent business facts, actors, computations or relationships.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from okf_tools import build_indexes, lint, migrate, normalize_verified, trust_tier, validate
from okf_tools.cli import main
from okf_tools.parser import iter_wikilinks, load_bundle, parse_document, split_frontmatter
from okf_tools.safety import BundleError, Limits, apply_changes, bundle_root

FIXTURES = Path(__file__).parent / "fixtures"
BUNDLES = ("acme_retail", "ga4", "crypto_bitcoin", "stackoverflow")
REVENUE = Path("computations/revenue-ytd.md")
POLICY = Path("policies/revenue-recognition.md")


@pytest.fixture
def bundle(tmp_path):
    return Path(shutil.copytree(FIXTURES / "acme_retail", tmp_path / "bundle"))


def current_bundle(path: Path) -> Path:
    payload = json.loads((FIXTURES / "upstream.json").read_text())
    for rel, record in payload["files"].items():
        assert hashlib.sha256(record["text"].encode()).hexdigest() == record["sha256"]
        if rel.startswith("bundles/acme_retail/"):
            destination = path / rel.removeprefix("bundles/acme_retail/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(record["text"], encoding="utf-8")
    return path


def parts(path: Path):
    metadata, body, error = split_frontmatter(path.read_text(encoding="utf-8"))
    assert error is None
    return metadata, body


def store(path: Path, metadata, body: str) -> None:
    """Reserialize source-derived metadata while preserving source body content."""
    path.write_text(
        "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n" + body, encoding="utf-8"
    )


def hashes(path: Path) -> dict[str, str]:
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("name", BUNDLES)
def test_reference_bundles_are_conformant(name):
    assert validate(FIXTURES / name).conformant


@pytest.mark.parametrize("name", BUNDLES)
def test_reference_bundles_have_no_lint_errors(name):
    assert not [f for f in lint(FIXTURES / name) if f.severity == "error"]


def test_current_upstream_and_legacy_freshness_boundaries(bundle):
    historical, _ = parts(bundle / REVENUE)
    current_bundle(bundle)
    metadata, _ = parts(bundle / REVENUE)
    deadline = metadata["stale_after"]
    assert isinstance(deadline, datetime)
    before = lint(bundle, today=deadline - timedelta(microseconds=1))
    at = lint(bundle, today=deadline)
    assert not [f for f in before if f.path == REVENUE.as_posix() and f.rule == "stale"]
    assert [f for f in at if f.path == REVENUE.as_posix() and f.rule == "stale"]
    assert validate(bundle).conformant
    assert not [f for f in before if f.severity == "error"]
    metadata, body = parts(bundle / REVENUE)
    metadata["stale_after"] = historical["stale_after"]
    store(bundle / REVENUE, metadata, body)
    midnight = datetime.combine(historical["stale_after"], time(), timezone.utc)
    assert [
        f
        for f in lint(bundle, today=midnight)
        if f.path == REVENUE.as_posix() and f.rule == "stale"
    ]


def test_naive_upstream_timestamp_is_diagnostic(bundle):
    current_bundle(bundle)
    metadata, body = parts(bundle / REVENUE)
    metadata["stale_after"] = metadata["stale_after"].replace(tzinfo=None)
    store(bundle / REVENUE, metadata, body)
    assert [
        f for f in lint(bundle) if f.path == REVENUE.as_posix() and f.rule == "invalid-stale-after"
    ]


def test_shared_snapshot_retains_one_consistent_document_view(bundle):
    snapshot = load_bundle(bundle)
    metadata, body = parts(bundle / REVENUE)
    del metadata["type"]
    store(bundle / REVENUE, metadata, body)
    assert validate(snapshot).conformant
    assert not validate(bundle).conformant
    assert not [f for f in lint(snapshot) if f.rule == "missing-type"]
    assert [f for f in lint(bundle) if f.rule == "missing-type"]


@pytest.mark.parametrize("operation", [validate, lint, migrate, build_indexes])
def test_public_operations_reject_non_directory(operation):
    with pytest.raises(BundleError):
        operation(FIXTURES / "SPEC.md")


def test_conformance_checks_missing_type_and_malformed_yaml(bundle):
    path = bundle / REVENUE
    metadata, body = parts(path)
    del metadata["type"]
    store(path, metadata, body)
    assert any(f.criterion == 2 and f.path == REVENUE.as_posix() for f in validate(bundle).failures)
    # Remove the actual source's closing delimiter: the document is now incomplete.
    original = (FIXTURES / "acme_retail" / REVENUE).read_text()
    path.write_text(original.replace("\n---\n", "\n", 1))
    assert any(f.criterion == 1 and f.path == REVENUE.as_posix() for f in validate(bundle).failures)
    result = migrate(bundle)
    assert REVENUE.as_posix() in result.skipped
    assert REVENUE.as_posix() not in result.changes


def test_reference_log_and_index_have_different_frontmatter_rules(bundle):
    assert validate(bundle).conformant
    log_text = (bundle / "log.md").read_text()
    (bundle / "metrics/index.md").write_text(log_text)
    assert any(f.criterion == 3 and f.path == "metrics/index.md" for f in validate(bundle).failures)


def test_missing_attester_breaks_lint_not_conformance(bundle):
    (bundle / "attesters/sql_equality.py").unlink()
    assert validate(bundle).conformant
    assert [
        f
        for f in lint(bundle)
        if f.rule == "broken-frontmatter-path" and "attester.resource" in f.message
    ]


def test_known_metadata_shapes_do_not_crash_or_disappear(bundle):
    metadata, body = parts(bundle / REVENUE)
    orders, _ = parts(bundle / "tables/orders.md")
    metadata["aliases"] = orders["sources"][0]["usage_count"]
    metadata["verified"] = metadata["verified"][0]["by"]
    metadata["sources"] = metadata["sources"][0]["resource"]
    store(bundle / REVENUE, metadata, body)
    failures = [f for f in lint(bundle) if f.path == REVENUE.as_posix() and f.severity == "error"]
    assert any("aliases" in f.message for f in failures)
    assert any("verified" in f.message for f in failures)
    assert any("sources" in f.message for f in failures)


def test_verified_mapping_normalization_and_advisory_tiers(bundle):
    metadata, _ = parts(bundle / REVENUE)
    assert trust_tier(metadata) == "human-reviewed"
    event = metadata["verified"][0]
    metadata["verified"] = event
    assert normalize_verified(metadata) == [event]
    assert trust_tier(metadata) == "human-reviewed"
    del metadata["verified"]
    assert trust_tier(metadata) == "unverified"


def test_code_examples_are_not_graph_edges():
    spec = parse_document(FIXTURES / "SPEC.md", FIXTURES)
    # The specification's customers link is inside its fenced document example.
    assert not [link for link in spec.links if link.href == "/tables/customers.md"]


def test_symlinked_descendants_are_rejected_before_any_apply(bundle, tmp_path):
    outside = tmp_path / "outside.md"
    shutil.copy2(bundle / "metrics/index.md", outside)
    destination = bundle / "metrics/index.md"
    destination.unlink()
    destination.symlink_to(outside)
    before = outside.read_bytes()
    for operation in [validate, lint, migrate, build_indexes]:
        with pytest.raises(BundleError):
            operation(bundle)
    for operation in [migrate, build_indexes]:
        with pytest.raises(BundleError):
            operation(bundle, apply=True)
    assert outside.read_bytes() == before


def test_absolute_link_resolution_canonicalizes_symlink_target(bundle, tmp_path):
    document = parse_document(bundle / POLICY, bundle)
    link = next(link for link in document.links if link.href == "/tables/orders.md")
    outside = tmp_path / "tables"
    shutil.move(str(bundle / "tables"), outside)
    (bundle / "tables").symlink_to(outside, target_is_directory=True)
    assert document.resolve(link) == (outside / "orders.md").resolve()
    with pytest.raises(BundleError):
        lint(bundle)


def test_resource_limits_reject_actual_document_before_parsing(bundle):
    size = (bundle / REVENUE).stat().st_size
    with pytest.raises(BundleError) as exc:
        validate(bundle, limits=Limits(max_file_bytes=size - 1))
    assert exc.value.code == "resource_limit"
    with pytest.raises(BundleError) as exc:
        lint(bundle, limits=Limits(max_findings=1))
    assert exc.value.code == "resource_limit"


def test_index_covers_navigation_and_underscore_concepts(bundle):
    page = bundle / "metrics/gross-margin-legacy.md"
    page.rename(page.with_name("_" + page.name))
    output = build_indexes(bundle)
    assert "index.md" in output
    assert "metrics/index.md" in output["index.md"]
    assert "_gross-margin-legacy.md" in output["metrics/index.md"]


def test_index_preserves_nested_navigation_and_is_stable(tmp_path):
    path = Path(shutil.copytree(FIXTURES / "stackoverflow", tmp_path / "bundle"))
    before = hashes(path)
    preview = build_indexes(path)
    assert hashes(path) == before
    assert "joins/index.md" in preview["references/index.md"]
    assert "metrics/index.md" in preview["references/index.md"]
    build_indexes(path, apply=True)
    after = hashes(path)
    build_indexes(path, apply=True)
    assert hashes(path) == after
    assert validate(path).conformant
    assert not [f for f in lint(path) if f.severity == "error"]


def test_migration_preserves_metadata_and_delimiter_without_body_blank(bundle):
    path = bundle / REVENUE
    metadata, body = parts(path)
    del metadata["description"]
    # Preserve actual sanctioned SQL as an unknown multiline extension value.
    metadata["computation_source"] = body.split("```sql\n", 1)[1].split("```", 1)[0]
    store(path, metadata, body.lstrip("\n"))
    original = path.read_bytes()
    preview = migrate(bundle)
    assert path.read_bytes() == original
    assert REVENUE.as_posix() in preview.changes
    result = migrate(bundle, apply=True)
    migrated_metadata, migrated_body = parts(path)
    assert REVENUE.as_posix() in result.applied
    assert all(migrated_metadata[key] == value for key, value in metadata.items())
    assert migrated_body == body.lstrip("\n")
    assert validate(bundle).conformant
    assert migrate(bundle).files_changed == 0


def test_delimiters_inside_actual_reference_text_are_metadata(bundle):
    path = bundle / REVENUE
    metadata, body = parts(path)
    reference = (FIXTURES / "SPEC.md").read_text()
    metadata["reference_text"] = reference
    del metadata["description"]
    store(path, metadata, body)
    parsed, _ = parts(path)
    assert parsed["reference_text"] == reference
    migrate(bundle, apply=True)
    migrated, _ = parts(path)
    assert migrated["reference_text"] == reference


def test_headerless_actual_document_is_reported_not_falsely_migrated(bundle):
    path = bundle / REVENUE
    _, body = parts(path)
    path.write_text(body.lstrip("\n"))
    original = path.read_bytes()
    result = migrate(bundle, apply=True)
    assert REVENUE.as_posix() in result.skipped
    assert path.read_bytes() == original


def test_wikilink_conversion_preserves_actual_relationship_and_code(bundle):
    path = bundle / POLICY
    original = path.read_text()
    target = "tables/orders"
    # Represent one existing relationship using Obsidian syntax; no new relationship.
    doc = parse_document(path, bundle)
    link = next(link for link in doc.links if link.href == "/tables/orders.md")
    old = re.search(r"\[[^\]]+\]\(" + re.escape(link.href) + r"\)", original).group()
    wiki = f"[[{target}|{link.text}]]"
    path.write_text(original.replace(old, wiki, 1))
    assert [item for item in lint(bundle) if item.rule == "wikilink"]
    result = migrate(bundle, apply=True)
    assert result.links_converted == 1
    converted = parse_document(path, bundle)
    assert any(item.href == link.href and item.text == link.text for item in converted.links)
    assert wiki not in path.read_text()
    # The same source relationship quoted as code is literal, not another graph edge.
    path.write_text(original.replace(old, "`" + wiki + "`", 1))
    before = path.read_bytes()
    assert migrate(bundle, apply=True).links_converted == 0
    assert path.read_bytes() == before


def test_wikilink_ambiguity_is_reported_without_guessing(bundle):
    path = bundle / POLICY
    document = parse_document(path, bundle)
    link = next(link for link in document.links if link.href == "/tables/orders.md")
    original = path.read_text()
    wiki = "[[" + Path(link.href).stem + "|" + link.text + "]]"
    old = re.search(r"\[[^\]]+\]\(" + re.escape(link.href) + r"\)", original).group()
    path.write_text(original.replace(old, wiki, 1))
    # Duplicate an actual target into another directory, exposing basename ambiguity.
    shutil.copy2(bundle / "tables/orders.md", bundle / "metrics/orders.md")
    assert [item for item in lint(bundle) if item.rule == "ambiguous-wikilink"]
    before = path.read_bytes()
    result = migrate(bundle, apply=True)
    assert "orders" in result.ambiguous
    assert set(result.ambiguous["orders"]) == {"tables/orders.md", "metrics/orders.md"}
    assert path.read_bytes() == before


def test_changed_input_rejects_entire_planned_write(bundle):
    first = "metrics/index.md"
    second = "policies/index.md"
    originals = {rel: (bundle / rel).read_text() for rel in (first, second)}
    changes = {first: originals[second], second: originals[first]}
    # A real index copied over another represents an intervening editor save.
    (bundle / second).write_text(originals[first])
    before = hashes(bundle)
    with pytest.raises(BundleError) as exc:
        apply_changes(bundle_root(bundle), changes, originals)
    assert exc.value.code == "changed_input"
    assert exc.value.applied == []
    assert hashes(bundle) == before


def test_source_and_actor_requirements_are_diagnostics(bundle):
    metadata, body = parts(bundle / REVENUE)
    del metadata["sources"][0]["resource"]
    del metadata["generated"]["by"]
    del metadata["verified"][0]["by"]
    store(bundle / REVENUE, metadata, body)
    rules = {f.rule for f in lint(bundle) if f.path == REVENUE.as_posix()}
    assert {"incomplete-source", "incomplete-generated", "incomplete-verified"} <= rules


def test_lost_attribution_is_not_silently_accepted(bundle):
    metadata, body = parts(bundle / REVENUE)
    source_id = metadata["sources"][0]["id"]
    del metadata["sources"][0]
    body = "\n".join(line for line in body.split("\n") if not line.startswith(f"[^{source_id}]:"))
    store(bundle / REVENUE, metadata, body)
    assert [
        f for f in lint(bundle) if f.rule == "dangling-footnote" and f.path == REVENUE.as_posix()
    ]


def test_reference_deprecation_distinguishes_supersession_from_duplicate(bundle):
    metadata, body = parts(bundle / "tables/orders.md")
    old_path = bundle / "metrics/gross-margin-legacy.md"
    old_metadata, _ = parts(old_path)
    metadata["status"] = old_metadata["status"]
    store(old_path, metadata, body)
    assert not [f for f in lint(bundle) if f.rule == "duplicate-concept"]
    metadata["status"] = parts(bundle / "tables/orders.md")[0]["status"]
    store(old_path, metadata, body)
    assert [f for f in lint(bundle) if f.rule == "duplicate-concept"]


def test_cli_json_errors_and_severity_policy(bundle, capsys):
    assert main(["validate", str(FIXTURES / "SPEC.md"), "--format", "json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error" and result["error"]["code"]
    assert (
        main(["lint", str(bundle), "--format", "json", "--severity", "error", "--fail-on", "info"])
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["counts"]["info"] > 0
    assert result["exit_code"] == 1
    assert main(["validate", str(bundle), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["conformant"] is True


def test_html_nesting_obeys_limits_on_actual_reference_visualization():
    html = (FIXTURES / "acme_retail/viz.html").read_text()
    with pytest.raises(BundleError, match="HTML nesting limit"):
        list(iter_wikilinks(html, limits=Limits(max_depth=1)))


def test_index_preview_and_apply_enforce_the_same_output_limit(tmp_path):
    metadata, body = parts(FIXTURES / "acme_retail" / REVENUE)
    # Subsample the actual concept to its metadata and first body line. Folding
    # its original description produces short input lines but a longer index entry.
    text = "---\n" + yaml.safe_dump(metadata, sort_keys=False, width=50) + "---\n"
    text += body.splitlines()[0] + "\n"
    path = tmp_path / REVENUE.name
    path.write_text(text)
    limits = Limits(max_line_bytes=max(len(line.encode()) for line in text.splitlines()))
    for apply in (False, True):
        with pytest.raises(BundleError, match="line byte limit"):
            build_indexes(tmp_path, apply=apply, limits=limits)
        assert not (tmp_path / "index.md").exists()
        assert path.read_text() == text
