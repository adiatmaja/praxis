"""stdio entry point: ``python -m mcp_server`` / ``praxis-mcp``."""

from __future__ import annotations

from mcp_server.server import mcp


def main() -> None:
    """Run the Praxis MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
