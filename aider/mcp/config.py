"""Discover MCP servers from config sources.

Merges servers found in (lowest to highest precedence):
  * ``.mcp.json`` in the home directory, git root, then cwd
  * ``.aider.conf.yml`` / CLI flags (``--mcp-server`` / ``--mcp-header``)
  * ``AIDER_MCP_SERVER`` environment variable
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class MCPServerConfig:
    name: str
    url: Optional[str] = None
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)
    env: Optional[dict[str, str]] = None
    headers: dict[str, str] = field(default_factory=dict)
    source: str = ""


def parse_name_value(text: str) -> Optional[tuple[str, str]]:
    """Parse ``name=value`` (first ``=`` splits name and value)."""
    text = text.strip().strip("\"'")
    if "=" not in text:
        return None
    name, _, value = text.partition("=")
    name = name.strip()
    value = value.strip().strip("\"'")
    if not name or not value:
        return None
    return name, value


def parse_header_value(text: str) -> Optional[tuple[str, str, str]]:
    """Parse ``name=Key:Value`` into (name, key, value)."""
    pair = parse_name_value(text)
    if not pair:
        return None
    name, rest = pair
    if ":" not in rest:
        return None
    key, _, value = rest.partition(":")
    return name, key.strip(), value.strip()


def _find_mcp_json(root: Optional[Path]) -> list[Path]:
    """Return candidate .mcp.json paths in precedence order (low to high)."""
    candidates = []
    if root:
        candidates.append(Path(root) / ".mcp.json")
    candidates += [
        Path.cwd() / ".mcp.json",
        Path.home() / ".mcp.json",
    ]
    seen = []
    for path in candidates:
        if path not in seen:
            seen.append(path)
    return seen


def _server_from_json(name: str, data: dict, source: str) -> MCPServerConfig:
    t = data.get("type", "")
    url = data.get("url")
    command = data.get("command")
    headers = data.get("headers") or {}

    if url and t != "stdio":
        return MCPServerConfig(
            name=name,
            url=str(url),
            headers={str(k): str(v) for k, v in headers.items()},
            source=source,
        )
    if command:
        return MCPServerConfig(
            name=name,
            command=str(command),
            args=[str(a) for a in data.get("args", [])],
            env=data.get("env"),
            source=source,
        )
    return MCPServerConfig(name=name, source=source)


def load_mcp_json(root: Optional[Path]) -> dict[str, MCPServerConfig]:
    """Load servers from .mcp.json files (home, git root, then cwd win)."""
    servers: dict[str, MCPServerConfig] = {}
    for path in _find_mcp_json(root):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        mcp_servers = data.get("mcpServers") or {}
        for name, spec in mcp_servers.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("enabled") is False:
                continue
            server = _server_from_json(name, spec, str(path))
            servers[name] = server
    return servers


def load_from_args(
    mcp_servers: Optional[list[str]],
    mcp_headers: Optional[list[str]],
    root: Optional[Path],
) -> dict[str, MCPServerConfig]:
    """Merge config-file/CLI and .mcp.json servers, CLI winning on conflicts."""
    servers: dict[str, MCPServerConfig] = {}

    for name, server in load_mcp_json(root).items():
        servers[name] = server

    # env var AIDER_MCP_SERVER="name=url,name2=url2"
    env_servers = os.environ.get("AIDER_MCP_SERVER", "")
    for chunk in env_servers.split(","):
        pair = parse_name_value(chunk)
        if pair:
            name, value = pair
            servers[name] = MCPServerConfig(
                name=name, url=value, source="AIDER_MCP_SERVER"
            )

    if mcp_servers:
        for item in mcp_servers:
            pair = parse_name_value(item)
            if pair:
                name, value = pair
                servers[name] = MCPServerConfig(
                    name=name,
                    url=value if value.startswith("http") else None,
                    command=None if value.startswith("http") else value,
                    source="--mcp-server",
                )

    if mcp_headers:
        for item in mcp_headers:
            parsed = parse_header_value(item)
            if parsed:
                name, key, value = parsed
                server = servers.get(name)
                if not server:
                    server = MCPServerConfig(name=name, source="--mcp-header")
                    servers[name] = server
                server.headers[key] = value

    return servers