"""Optional, read-only stdio MCP adapter with server-configured filesystem roots."""

import argparse
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any, Literal, Sequence

from . import __version__
from .results import dumps, error_result, execute
from .safety import DEFAULT_LIMITS, BundleError, Limits, bundle_root

DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
_MIN_RESPONSE_BYTES = 4096
_INSTALL_HELP = "Use Python >=3.11 and install the optional SDK: pip install 'okf-tools[mcp]'"


def _sdk() -> tuple[Any, ...]:
    if sys.version_info < (3, 11):
        raise BundleError(_INSTALL_HELP, code="mcp_unavailable")
    try:
        sdk_version = version("mcp")
        parts = sdk_version.split(".")
        if int(parts[0]) != 1 or int(parts[1]) < 30:
            raise BundleError(
                f"Unsupported mcp SDK {sdk_version}; require mcp>=1.30,<2. {_INSTALL_HELP}",
                code="mcp_unavailable",
            )
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError
        from pydantic import ValidationError
        from mcp.types import CallToolResult, TextContent, ToolAnnotations
    except (ImportError, PackageNotFoundError, ValueError, IndexError) as error:
        if isinstance(error, BundleError):
            raise
        raise BundleError(_INSTALL_HELP, code="mcp_unavailable") from error
    return FastMCP, ToolAnnotations, CallToolResult, TextContent, ToolError, ValidationError


def _bounded(result: dict[str, Any], max_response_bytes: int) -> dict[str, Any]:
    # FastMCP v1 emits both a pretty-printed text block and structuredContent.
    # Escaping that text can at most double its size; reserve the third copy and
    # protocol-result overhead rather than capping only the smaller domain JSON.
    size = 0
    try:
        for chunk in json.JSONEncoder(ensure_ascii=False, indent=2, allow_nan=False).iterencode(
            result
        ):
            size += len(chunk.encode("utf-8"))
            if 3 * size + 1024 > max_response_bytes:
                return error_result(
                    result["operation"],
                    None,
                    BundleError(
                        "response byte limit exceeded; request a smaller bundle or raise the "
                        "server's --max-response-bytes limit",
                        code="response_limit",
                    ),
                )
    except UnicodeError:
        return error_result(
            result["operation"],
            None,
            BundleError(
                "response contains a path that cannot be encoded as UTF-8",
                code="invalid_encoding",
            ),
        )
    return result


def create_server(
    roots: Sequence[str | Path],
    *,
    limits: Limits = DEFAULT_LIMITS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> Any:
    """Construct four read-only tools; no client input can widen configured roots.

    A caller must exclude concurrent directory-tree mutation during each request,
    as with the Python and CLI APIs. Response overflow is an explicit failure,
    never a truncated successful result.
    """
    if not roots:
        raise BundleError(
            "configure at least one allowed directory with --root", code="missing_roots"
        )
    if (
        not isinstance(max_response_bytes, int)
        or isinstance(max_response_bytes, bool)
        or max_response_bytes < _MIN_RESPONSE_BYTES
    ):
        raise BundleError(
            f"max_response_bytes must be an integer >= {_MIN_RESPONSE_BYTES}",
            code="invalid_argument",
        )
    allowed_roots = tuple(dict.fromkeys(bundle_root(root) for root in roots))
    FastMCP, ToolAnnotations, CallToolResult, TextContent, ToolError, ValidationError = _sdk()
    operations = {
        "validate_bundle": "validate",
        "lint_bundle": "lint",
        "preview_indexes": "index",
        "preview_migration": "migrate",
    }

    def response(result: dict[str, Any]) -> CallToolResult:
        result = _bounded(result, max_response_bytes)
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        result,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                        allow_nan=False,
                    ),
                )
            ],
            structuredContent=result,
            isError=result["exit_code"] == 2,
        )

    class BoundedFastMCP(FastMCP):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            # SDK dispatch/argument failures occur before request(). Keep them
            # within the same envelope and budget, without echoing client input.
            if name not in operations:
                return response(
                    error_result(
                        "mcp",
                        None,
                        BundleError(
                            "unknown MCP tool",
                            code="unknown_tool",
                        ),
                    )
                )
            try:
                return await super().call_tool(name, arguments)
            except ToolError as error:
                invalid = isinstance(error.__cause__, ValidationError)
                return response(
                    error_result(
                        operations[name],
                        None,
                        BundleError(
                            "invalid tool arguments" if invalid else "tool execution failed",
                            code="invalid_argument" if invalid else "tool_error",
                        ),
                    )
                )

    server = BoundedFastMCP(
        "okf-tools",
        instructions=(
            "Read-only OKF bundle validation, lint, and migration/index previews. "
            "Bundle paths must be within a server-configured allowed root. "
            "Results use the same versioned envelope as okf --format json. "
            "Inspect status and exit_code: violations are not operational errors. "
            "This server never applies changes or fetches network resources."
        ),
        log_level="ERROR",
    )
    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    def request(
        operation: str, path: str, *, actor: str | None = None, fail_on: str = "error"
    ) -> CallToolResult:
        try:
            # Authorize the resolved path before reading any bundle contents.
            canonical = Path(path).resolve()
            if not any(canonical.is_relative_to(root) for root in allowed_roots):
                raise BundleError(
                    "bundle path is outside the server's allowed roots",
                    code="forbidden_path",
                    path=path,
                )
            result = execute(operation, canonical, actor=actor, fail_on=fail_on, limits=limits)
        except BundleError as error:
            result = error_result(operation, path, error)
        except (OSError, RuntimeError, ValueError) as error:
            result = error_result(
                operation,
                path,
                BundleError(
                    str(error),
                    code="invalid_bundle",
                    path=path,
                ),
            )
        return response(result)

    @server.tool(annotations=annotations, structured_output=True)
    def validate_bundle(path: str) -> Annotated[CallToolResult, dict[str, Any]]:
        """Check OKF conformance for a bundle in an allowed root; never modify files."""
        return request("validate", path)

    @server.tool(annotations=annotations, structured_output=True)
    def lint_bundle(
        path: str,
        fail_on: Literal["error", "warning", "info"] = "error",
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        """Return all hygiene findings; fail_on selects the failure threshold, not visibility."""
        return request("lint", path, fail_on=fail_on)

    @server.tool(annotations=annotations, structured_output=True)
    def preview_indexes(path: str) -> Annotated[CallToolResult, dict[str, Any]]:
        """Return proposed index.md contents for a bundle without writing anything."""
        return request("index", path)

    @server.tool(annotations=annotations, structured_output=True)
    def preview_migration(
        path: str,
        actor: str | None = None,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        """Preview Obsidian migration, including skipped and unresolved work; never write files."""
        return request("migrate", path, actor=actor)

    return server


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BundleError(message, code="invalid_argument")

    def _print_message(self, message: str | None, file: Any = None) -> None:
        # Even help/version output stays off the stdio protocol channel.
        super()._print_message(message, sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(prog="okf-mcp", description="Serve read-only OKF tools over stdio.")
    parser.add_argument("--version", action="version", version=f"okf-tools {__version__}")
    parser.add_argument(
        "--root",
        action="append",
        type=Path,
        required=True,
        help="allowed bundle directory (repeat to allow more roots)",
    )
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=DEFAULT_MAX_RESPONSE_BYTES,
        help=f"maximum tool response size in bytes (default: {DEFAULT_MAX_RESPONSE_BYTES})",
    )
    for name, value in vars(DEFAULT_LIMITS).items():
        parser.add_argument(f"--{name.replace('_', '-')}", type=int, default=value)
    try:
        args = parser.parse_args(argv)
        limits = Limits(**{name: getattr(args, name) for name in vars(DEFAULT_LIMITS)})
        server = create_server(args.root, limits=limits, max_response_bytes=args.max_response_bytes)
        server.run(transport="stdio")
    except BundleError as error:
        print(dumps(error_result("mcp", None, error)), file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(
            dumps(
                error_result(
                    "mcp",
                    None,
                    BundleError(
                        str(error),
                        code="invalid_argument",
                        path=getattr(error, "filename", None),
                    ),
                )
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
