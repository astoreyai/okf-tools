"""Command line interface for okf-tools."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .indexer import build_indexes
from .lint import lint
from .migrate import migrate
from .validate import validate

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
COLOR = {"error": RED, "warning": YELLOW, "info": DIM}


def _no_color(stream) -> bool:
    return not stream.isatty()


def _c(text: str, color: str) -> str:
    return text if _no_color(sys.stdout) else f"{color}{text}{RESET}"


def cmd_validate(args: argparse.Namespace) -> int:
    report = validate(args.bundle)
    print(f"OKF v0.1 conformance: {args.bundle}")
    print(f"  concept documents : {report.concepts}")
    print(f"  reserved files    : {report.reserved}")
    if report.conformant:
        print(_c("\n  CONFORMANT (section 9: all three criteria pass)", GREEN))
        return 0
    print(f"\n  {_c('NOT CONFORMANT', RED)} ({len(report.failures)} failure(s))\n")
    for f in report.failures:
        print(f"  [criterion {f.criterion}] {f.path}: {f.message}")
    return 1


def cmd_lint(args: argparse.Namespace) -> int:
    findings = lint(args.bundle)
    if args.severity:
        order = {"error": 0, "warning": 1, "info": 2}
        findings = [f for f in findings if order[f.severity] <= order[args.severity]]
    if not findings:
        print(_c("clean: no findings", GREEN))
        return 0
    counts = {s: sum(1 for f in findings if f.severity == s) for s in ("error", "warning", "info")}
    for f in findings:
        loc = f"{f.path}:{f.line}" if f.line else f.path
        tag = _c(f.severity.upper().ljust(7), COLOR[f.severity])
        print(f"{tag} {loc}  [{f.rule}]")
        print(f"        {f.message}")
    print(
        f"\n{counts['error']} error(s), {counts['warning']} warning(s), {counts['info']} info"
    )
    return 1 if counts["error"] else 0


def cmd_migrate(args: argparse.Namespace) -> int:
    result = migrate(args.bundle, apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY RUN (nothing written; pass --apply)"
    print(f"Obsidian -> OKF migration: {mode}")
    print(f"  files changed         : {result.files_changed}")
    print(f"  wikilinks converted   : {result.links_converted}")
    for k, v in sorted(result.fields_added.items()):
        print(f"  {k+' added':22s}: {v}")
    if result.unresolved:
        print(f"\n  {len(result.unresolved)} unresolved wikilink(s), left untouched.")
        print("  These point at pages that do not exist. Converting them would create dead")
        print("  links, which OKF consumers tolerate in silence. They are wanted pages:\n")
        for target in result.wanted_pages[:15]:
            print(f"    [[{target}]]")
        if len(result.wanted_pages) > 15:
            print(f"    ... and {len(result.wanted_pages) - 15} more")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    indexes = build_indexes(args.bundle, apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY RUN (nothing written; pass --apply)"
    print(f"OKF index.md generation: {mode}")
    for rel in sorted(indexes):
        print(f"  {rel}")
    print(f"\n  {len(indexes)} index file(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="okf",
        description="Validate, lint, migrate, and index Open Knowledge Format bundles.",
    )
    p.add_argument("--version", action="version", version=f"okf-tools {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="check OKF v0.1 section 9 conformance")
    v.add_argument("bundle", type=Path)
    v.set_defaults(func=cmd_validate)

    ln = sub.add_parser(
        "lint", help="find the failures conformance does not catch (broken links, wikilinks, forks)"
    )
    ln.add_argument("bundle", type=Path)
    ln.add_argument("--severity", choices=("error", "warning", "info"), default=None)
    ln.set_defaults(func=cmd_lint)

    m = sub.add_parser("migrate", help="convert an Obsidian vault into an OKF bundle")
    m.add_argument("bundle", type=Path)
    m.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    m.set_defaults(func=cmd_migrate)

    i = sub.add_parser("index", help="generate reserved index.md files")
    i.add_argument("bundle", type=Path)
    i.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    i.set_defaults(func=cmd_index)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.bundle.is_dir():
        print(f"not a directory: {args.bundle}", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
