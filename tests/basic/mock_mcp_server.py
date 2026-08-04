"""A tiny, deterministic MCP server used only for tests.

Exposes two tools (``echo_tool`` and ``add``) and is served either over
streamable HTTP (for in-process uvicorn) or stdio (as a subprocess).
"""

from mcp.server.fastmcp import FastMCP


def make_server() -> FastMCP:
    server = FastMCP("mock-aider-test")

    @server.tool()
    def echo_tool(text: str) -> str:
        """Echo the given text back."""
        return f"echo: {text}"

    @server.tool()
    def add(a: float, b: float) -> float:
        """Add two numbers."""
        return a + b

    return server


if __name__ == "__main__":
    make_server().run()
