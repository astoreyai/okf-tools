"""Transport-independent, versioned operation results for Python, CLI, and MCP."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import __version__
from .indexer import build_indexes
from .lint import SEVERITY_ORDER, lint
from .migrate import migrate
from .parser import BundleSnapshot, is_conventional_actor, load_bundle
from .safety import DEFAULT_LIMITS, BundleError, Limits
from .validate import Report, validate

SCHEMA_VERSION = "1"


def report_data(report: Report) -> dict[str, Any]:
    """Serialize the complete conformance contract, including its computed property."""
    return {**asdict(report), "conformant": report.conformant}


def _result(
    operation: str,
    bundle: str | None,
    data: dict[str, Any] | None,
    exit_code: int,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "okf-tools", "version": __version__},
        "operation": operation,
        "bundle": bundle,
        "status": {0: "ok", 1: "violations", 2: "error"}[exit_code],
        "exit_code": exit_code,
        "data": data,
        "error": error,
    }


def error_result(operation: str, bundle: str | Path | None, error: BundleError) -> dict[str, Any]:
    """Represent an operational failure without disguising partial writes."""
    return _result(
        operation,
        str(bundle) if bundle is not None else None,
        None,
        2,
        {
            "code": error.code,
            "message": str(error),
            "path": error.path,
            "applied": error.applied,
        },
    )


def dumps(result: dict[str, Any]) -> str:
    """Emit deterministic JSON without transport-specific objects."""
    return json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)


def execute(
    operation: str,
    bundle: Path | BundleSnapshot,
    *,
    apply: bool = False,
    actor: str | None = None,
    fail_on: str = "error",
    limits: Limits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Run one operation with one bounded snapshot and a shared exit/status policy.

    Lint always returns every finding. Display filtering belongs to the renderer,
    never to the failure threshold. Migration is incomplete while any work is
    skipped, ambiguous, or unresolved, even if other changes were applied.
    """
    location = str(bundle.root if isinstance(bundle, BundleSnapshot) else bundle)
    try:
        if operation not in ("validate", "lint", "migrate", "index"):
            raise BundleError(f"unknown operation: {operation}", code="invalid_argument")
        if apply and operation not in ("migrate", "index"):
            raise BundleError("this operation does not write files", code="invalid_argument")
        if fail_on not in SEVERITY_ORDER:
            raise BundleError("fail_on must be error, warning, or info", code="invalid_argument")
        if actor is not None and not is_conventional_actor(actor):
            raise BundleError(
                "actor must use human:<id>, process:<id>, team:<id>, or <producer>/<version>",
                code="invalid_actor",
            )
        snapshot = load_bundle(bundle, limits=limits)
        location = str(snapshot.root)
        if operation == "validate":
            report = validate(snapshot, limits=limits)
            return _result(operation, location, report_data(report), int(not report.conformant))
        if operation == "lint":
            findings = lint(snapshot, limits=limits)
            counts = {severity: 0 for severity in SEVERITY_ORDER}
            for finding in findings:
                counts[finding.severity] += 1
            failed = any(
                counts[s] and order <= SEVERITY_ORDER[fail_on]
                for s, order in SEVERITY_ORDER.items()
            )
            return _result(
                operation,
                location,
                {
                    "findings": [asdict(finding) for finding in findings],
                    "counts": counts,
                    "fail_on": fail_on,
                },
                int(failed),
            )
        if operation == "migrate":
            migration = migrate(snapshot, apply=apply, actor=actor, limits=limits)
            data = asdict(migration)
            # Tuples in the Python result become JSON arrays in every transport.
            data["unresolved"] = [list(link) for link in migration.unresolved]
            data["wanted_pages"] = migration.wanted_pages
            data["complete"] = not (
                migration.skipped or migration.ambiguous or migration.unresolved
            )
            data["mode"] = "apply" if apply else "preview"
            return _result(operation, location, data, int(not data["complete"]))
        indexes = build_indexes(snapshot, apply=apply, limits=limits)
        originals = {document.rel: document.raw for document in snapshot.documents}
        changed = [path for path, content in indexes.items() if originals.get(path) != content]
        return _result(
            operation,
            location,
            {
                "mode": "apply" if apply else "preview",
                "changes": indexes,
                "changed_paths": changed,
                "applied": changed if apply else [],
            },
            0,
        )
    except BundleError as error:
        return error_result(operation, location, error)
    except (OSError, UnicodeError) as error:
        return error_result(
            operation,
            location,
            BundleError(
                str(error),
                code="io_error",
                path=getattr(error, "filename", None) or location,
            ),
        )
    except ValueError as error:
        return error_result(
            operation,
            location,
            BundleError(
                str(error),
                code="invalid_argument",
                path=location,
            ),
        )
