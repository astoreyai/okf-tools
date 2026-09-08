"""Command line interface for okf-tools."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .lint import SEVERITY_ORDER
from .results import dumps, error_result, execute
from .safety import BundleError

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
COLOR = {"error": RED, "warning": YELLOW, "info": DIM}


def _c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if sys.stdout.isatty() else text


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BundleError(message, code="invalid_argument")


def _print_changes(changes: dict[str, str]) -> None:
    """Identify every proposed file and show a bounded excerpt of its contents."""
    for path, content in sorted(changes.items()):
        lines = content.splitlines()
        print(f"\n  proposed {path}: {len(lines)} line(s), {len(content.encode('utf-8'))} bytes")
        for line in lines[:12]:
            print(f"    {line[:160]}{' ...' if len(line) > 160 else ''}")
        if len(lines) > 12 or any(len(line) > 160 for line in lines[:12]):
            print("    ... excerpt only; --format json includes the complete proposed contents")


def _print_validate(result: dict[str, Any]) -> None:
    data = result["data"]
    print(f"OKF v0.2 conformance: {result['bundle']}")
    print(f"  concept documents : {data['concepts']}")
    print(f"  reserved files    : {data['reserved']}")
    if data["conformant"]:
        print(_c("\n  CONFORMANT (section 11: all three criteria pass)", GREEN))
    else:
        print(f"\n  {_c('NOT CONFORMANT', RED)} ({len(data['failures'])} failure(s))\n")
        for failure in data["failures"]:
            print(f"  [criterion {failure['criterion']}] {failure['path']}: {failure['message']}")


def _print_lint(result: dict[str, Any], severity: str | None) -> None:
    data = result["data"]
    findings = data["findings"]
    visible = [
        finding
        for finding in findings
        if severity is None or SEVERITY_ORDER[finding["severity"]] <= SEVERITY_ORDER[severity]
    ]
    if not findings:
        print(_c("clean: no findings", GREEN))
        return
    for finding in visible:
        loc = f"{finding['path']}:{finding['line']}" if finding["line"] else finding["path"]
        tag = _c(finding["severity"].upper().ljust(7), COLOR[finding["severity"]])
        print(f"{tag} {loc}  [{finding['rule']}]")
        print(f"        {finding['message']}")
    if len(visible) != len(findings):
        print(f"\n{len(findings) - len(visible)} finding(s) hidden by --severity")
    counts = data["counts"]
    print(f"\n{counts['error']} error(s), {counts['warning']} warning(s), {counts['info']} info")
    print(f"Failure threshold: {data['fail_on']} ({'failed' if result['exit_code'] else 'passed'})")


def _print_migrate(result: dict[str, Any]) -> None:
    data = result["data"]
    mode = "APPLIED" if data["mode"] == "apply" else "DRY RUN (nothing written; pass --apply)"
    print(f"Obsidian -> OKF migration: {mode}")
    print(f"  files changed         : {data['files_changed']}")
    print(f"  wikilinks converted   : {data['links_converted']}")
    for key, count in sorted(data["fields_added"].items()):
        print(f"  {key + ' added':22s}: {count}")
    for path, reason in sorted(data["skipped"].items()):
        print(f"  skipped {path}: {reason}")
    for target, candidates in sorted(data["ambiguous"].items()):
        print(f"  ambiguous [[{target}]]: {', '.join(candidates)}")
    for path, target in data["unresolved"]:
        print(f"  unresolved {path}: [[{target}]] (left untouched)")
    if data["wanted_pages"]:
        print(f"  wanted pages: {', '.join(data['wanted_pages'])}")
    if not data["complete"]:
        print(_c("\n  INCOMPLETE: skipped, ambiguous, or unresolved work remains", YELLOW))
    if data["mode"] == "preview":
        _print_changes(data["changes"])
    else:
        for path in data["applied"]:
            print(f"  applied {path}")


def _print_index(result: dict[str, Any]) -> None:
    data = result["data"]
    mode = "APPLIED" if data["mode"] == "apply" else "DRY RUN (nothing written; pass --apply)"
    print(f"OKF index.md generation: {mode}")
    if data["mode"] == "preview":
        _print_changes(data["changes"])
    else:
        for path in data["applied"]:
            print(f"  applied {path}")
    print(f"\n  {len(data['changes'])} index file(s), {len(data['changed_paths'])} changed")


def _emit(result: dict[str, Any], format: str, severity: str | None = None) -> int:
    if format == "json":
        print(dumps(result))
    elif result["error"] is not None:
        error = result["error"]
        location = f" ({error['path']})" if error["path"] is not None else ""
        print(f"error[{error['code']}]{location}: {error['message']}", file=sys.stderr)
        for path in error["applied"]:
            print(f"  already applied: {path}", file=sys.stderr)
    elif result["operation"] == "validate":
        _print_validate(result)
    elif result["operation"] == "lint":
        _print_lint(result, severity)
    elif result["operation"] == "migrate":
        _print_migrate(result)
    else:
        _print_index(result)
    return result["exit_code"]


def cmd_validate(args: argparse.Namespace) -> int:
    return _emit(execute("validate", args.bundle), args.format)


def cmd_lint(args: argparse.Namespace) -> int:
    return _emit(execute("lint", args.bundle, fail_on=args.fail_on), args.format, args.severity)


def cmd_migrate(args: argparse.Namespace) -> int:
    return _emit(execute("migrate", args.bundle, apply=args.apply, actor=args.actor), args.format)


def cmd_index(args: argparse.Namespace) -> int:
    return _emit(execute("index", args.bundle, apply=args.apply), args.format)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="okf",
        description="Validate, lint, migrate, and index Open Knowledge Format bundles.",
    )
    parser.add_argument("--version", action="version", version=f"okf-tools {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate", help="check OKF v0.2 section 11 conformance")
    validate_parser.set_defaults(func=cmd_validate)
    lint_parser = sub.add_parser(
        "lint",
        help="find broken links, missing provenance, staleness, forks, and other hygiene issues",
    )
    lint_parser.add_argument(
        "--severity",
        choices=tuple(SEVERITY_ORDER),
        default=None,
        help="minimum severity displayed in text; JSON always retains all findings",
    )
    lint_parser.add_argument(
        "--fail-on",
        choices=tuple(SEVERITY_ORDER),
        default="error",
        help="minimum severity causing exit 1, independent of --severity (default: error)",
    )
    lint_parser.set_defaults(func=cmd_lint)
    migrate_parser = sub.add_parser("migrate", help="convert an Obsidian vault into an OKF bundle")
    migrate_parser.add_argument(
        "--apply", action="store_true", help="write changes (default: dry run)"
    )
    migrate_parser.add_argument(
        "--actor",
        default=None,
        metavar="ACTOR",
        help="section 7 actor (e.g. human:aaron) recorded as generated.by; without it, "
        "the derived date is written as the legacy timestamp",
    )
    migrate_parser.set_defaults(func=cmd_migrate)
    index_parser = sub.add_parser("index", help="generate reserved index.md files")
    index_parser.add_argument(
        "--apply", action="store_true", help="write changes (default: dry run)"
    )
    index_parser.set_defaults(func=cmd_index)
    for command in (validate_parser, lint_parser, migrate_parser, index_parser):
        command.add_argument("bundle", type=Path)
        command.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(arguments)
    except BundleError as error:
        # Parsing can fail before a Namespace exists; honor an explicitly requested JSON error.
        output_parser = _ArgumentParser(add_help=False)
        output_parser.add_argument("--format", choices=("text", "json"), default="text")
        try:
            output_args, _ = output_parser.parse_known_args(arguments)
            output_format = output_args.format
        except BundleError:
            output_format = "text"
        operation = next(
            (arg for arg in arguments if arg in ("validate", "lint", "migrate", "index")), "cli"
        )
        return _emit(error_result(operation, None, error), output_format)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
