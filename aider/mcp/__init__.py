"""MCP (Model Context Protocol) integration for Aider.

Provides helpers for loading MCP servers from configuration
(``.aider.conf.yml``, ``.mcp.json``, CLI flags and the ``/mcp`` command)
and executing their tools either automatically (via the LLM) or manually.
"""

from .manager import MCPManager

__all__ = ["MCPManager"]