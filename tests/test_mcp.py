"""Exercise the optional MCP adapter over real stdio, against vendored documents."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def response_data(result):
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def test_stdio_server_is_read_only_and_enforces_configured_root():
    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "okf_tools.mcp_server", "--root", str(FIXTURES / "acme_retail")],
        )
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert {tool.name for tool in tools} == {
                    "validate_bundle",
                    "lint_bundle",
                    "preview_indexes",
                    "preview_migration",
                }
                for tool in tools:
                    assert tool.annotations.readOnlyHint is True
                    assert "apply" not in tool.inputSchema.get("properties", {})
                accepted = await session.call_tool(
                    "validate_bundle", {"path": str(FIXTURES / "acme_retail")}
                )
                assert response_data(accepted)["data"]["conformant"] is True
                rejected = await session.call_tool(
                    "validate_bundle", {"path": str(FIXTURES / "crypto_bitcoin")}
                )
                denied = response_data(rejected)
                assert rejected.isError is True
                assert denied["exit_code"] == 2
                assert denied["data"] is None
                assert denied["error"]["code"]
                preview = await session.call_tool(
                    "preview_indexes", {"path": str(FIXTURES / "acme_retail")}
                )
                assert response_data(preview)["exit_code"] == 0

    asyncio.run(exercise())


def test_stdio_server_bounds_schema_rejections_and_unknown_tools():
    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "okf_tools.mcp_server",
                "--root",
                str(FIXTURES / "acme_retail"),
                "--max-response-bytes",
                "4096",
            ],
        )
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                # An actual upstream document is not a legal severity value. SDK
                # schema errors previously echoed it outside the response budget.
                rejected = await session.call_tool(
                    "lint_bundle",
                    {
                        "path": str(FIXTURES / "acme_retail"),
                        "fail_on": (FIXTURES / "SPEC.md").read_text(),
                    },
                )
                assert len(rejected.model_dump_json(exclude_none=True).encode()) <= 4096
                assert rejected.isError is True
                assert response_data(rejected)["error"]["code"] == "invalid_argument"
                unknown = await session.call_tool((FIXTURES / "SPEC.md").read_text(), {})
                assert len(unknown.model_dump_json(exclude_none=True).encode()) <= 4096
                assert unknown.isError is True
                assert response_data(unknown)["error"]["code"] == "unknown_tool"

    asyncio.run(exercise())
