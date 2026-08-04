"""MCPManager: multi-server registry + dispatch for Aider.

Exposes a merged tool list to the LLM (via ``function_definitions``) and
executes tool calls against the owning server. Handles result truncation,
error reflection, runtime add/remove and shutdown.
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.types import Tool

from .client import MCPClient
from .config import MCPServerConfig, load_from_args


class MCPManager:
    def __init__(
        self,
        io=None,
        output_limit: int = 16000,
        max_roundtrips: int = 5,
        timeout: float = 60,
    ):
        self.io = io
        self.output_limit = output_limit
        self.max_roundtrips = max_roundtrips
        self.timeout = timeout

        self._configs: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, MCPClient] = {}
        self._tool_to_server: dict[str, str] = {}  # tool name -> server name
        self._errors: dict[str, str] = {}  # server name -> error message
        self._runtime: dict[str, MCPServerConfig] = {}
        self._started = False
        self.config_root = None

    # -- lifecycle ---------------------------------------------------------

    def add_config(self, server: MCPServerConfig) -> None:
        self._configs[server.name] = server

    def load_args(
        self,
        mcp_servers: Optional[list[str]],
        mcp_headers: Optional[list[str]],
        root,
    ) -> None:
        self.config_root = root
        for name, server in load_from_args(mcp_servers, mcp_headers, root).items():
            self.add_config(server)

    def runtime_configs(self) -> list[MCPServerConfig]:
        return list(self._runtime.values()) + [
            c for n, c in self._configs.items() if n not in self._runtime
        ]

    def start(self) -> None:
        if self._started:
            return
        self._started = True

        for name, cfg in dict(self._configs).items():
            self._connect(cfg)

    def _connect(self, cfg: MCPServerConfig) -> bool:
        try:
            client = MCPClient(
                name=cfg.name,
                url=cfg.url,
                command=cfg.command,
                args=cfg.args,
                env=cfg.env,
                headers=cfg.headers,
                timeout=self.timeout,
            )
            client.start()
        except Exception as err:
            self._errors[cfg.name] = str(err)
            if self.io:
                self.io.tool_warning(f"Unable to connect to MCP server '{cfg.name}': {err}")
            return False

        self._clients[cfg.name] = client
        self._errors.pop(cfg.name, None)

        # register tools
        try:
            for tool in client.list_tools():
                self._tool_to_server[tool.name] = cfg.name
        except Exception as err:
            self._errors[cfg.name] = str(err)
            if self.io:
                self.io.tool_warning(
                    f"MCP server '{cfg.name}' connected but listed no tools: {err}"
                )
        return True

    def shutdown(self) -> None:
        for name, client in list(self._clients.items()):
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()
        self._tool_to_server.clear()
        self._started = False

    # -- status ------------------------------------------------------------

    @property
    def servers(self) -> list[str]:
        return sorted(set(self._configs) | set(self._runtime))

    def server_status(self) -> dict[str, str]:
        status = {}
        for name in self.servers:
            if name in self._clients:
                status[name] = "connected"
            elif name in self._errors:
                status[name] = f"error: {self._errors[name]}"
            else:
                status[name] = "down"
        return status

    # -- tools -------------------------------------------------------------

    def list_tools(self) -> list[Tool]:
        tools: dict[str, Tool] = {}
        for name, client in self._clients.items():
            try:
                for tool in client.list_tools():
                    tools.setdefault(tool.name, tool)
            except Exception:
                continue
        return list(tools.values())

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name in self._tool_to_server

    def function_definitions(self) -> list[dict[str, Any]]:
        """OpenAI-compatible function specs for all MCP tools."""
        functions = []
        for tool in self.list_tools():
            parameters = getattr(tool, "inputSchema", {})
            if hasattr(parameters, "model_dump"):
                parameters = parameters.model_dump()
            if isinstance(parameters, dict) and parameters.get("type") == "object":
                defn = {
                    "name": tool.name,
                    "description": getattr(tool, "description", ""),
                    "parameters": parameters,
                }
                functions.append(defn)
        return functions

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool via its owning server, return a result dict."""
        server_name = self._tool_to_server.get(tool_name)
        client = self._clients.get(server_name or "")
        if not client:
            return {
                "text": f"Error: unknown MCP tool '{tool_name}'",
                "is_error": True,
            }

        try:
            result = client.call_tool(tool_name, arguments or {})
        except Exception as err:
            return {"text": f"Error calling MCP tool '{tool_name}': {err}", "is_error": True}

        text = self._result_to_text(result)
        return {"text": text, "is_error": bool(getattr(result, "isError", False))}

    def _result_to_text(self, result) -> str:
        parts = []
        for block in getattr(result, "content", []) or []:
            block_type = getattr(block, "type", "")
            if block_type == "text" or hasattr(block, "text"):
                parts.append(str(getattr(block, "text", "")))
            else:
                data = getattr(block, "data", None)
                if data is not None:
                    parts.append(f"[{block_type} data, {len(data)} bytes]")
                else:
                    try:
                        parts.append(str(block))
                    except Exception:
                        parts.append(str(block_type))
        text = "\n".join(parts).strip()
        if not text:
            text = f"(tool '{getattr(result, 'name', '')}' returned no text output)"
        return text[: self.output_limit]

    # -- runtime mutations (TUI) -------------------------------------------

    def add_server(self, cfg: MCPServerConfig) -> bool:
        ok = self._connect(cfg)
        if ok:
            self._runtime[cfg.name] = cfg
            self._configs.setdefault(cfg.name, cfg)
        return ok

    def remove_server(self, name: str) -> bool:
        client = self._clients.pop(name, None)
        if client:
            try:
                client.close()
            except Exception:
                pass
        self._configs.pop(name, None)
        self._runtime.pop(name, None)
        self._errors.pop(name, None)
        for tool, server in list(self._tool_to_server.items()):
            if server == name:
                self._tool_to_server.pop(tool, None)
        return True