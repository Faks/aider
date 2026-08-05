"""Tests for MCP client, config discovery and manager.

Uses a local mock MCP server (``mock_mcp_server``) - never a real or
external MCP endpoint - so tests are deterministic and offline.
"""

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MOCK_SERVER = ROOT / "tests" / "basic" / "mock_mcp_server.py"

from aider.mcp.client import MCPClient
from aider.mcp.config import (
    MCPServerConfig,
    load_from_args,
    load_mcp_json,
    parse_header_value,
    parse_name_value,
)
from aider.mcp.manager import MCPManager


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server():
    """Start the mock server over streamable HTTP on an ephemeral port."""
    import importlib.util
    import uvicorn

    spec = importlib.util.spec_from_file_location("mock_mcp_server", MOCK_SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    app = mod.make_server().streamable_http_app()
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


class TestParse:
    def test_parse_name_value(self):
        assert parse_name_value("a=http://x") == ("a", "http://x")
        assert parse_name_value("noequals") is None

    def test_parse_header_value(self):
        assert parse_header_value('m="Authorization: Bearer tok"') == (
            "m",
            "Authorization",
            "Bearer tok",
        )


class TestConfig:
    def test_mcp_json_http(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {"mcpServers": {"s1": {"url": "http://x/mcp", "headers": {"Authorization": "Bearer t"}}}}
            )
        )
        servers = load_mcp_json(tmp_path)
        assert servers["s1"].url == "http://x/mcp"
        assert servers["s1"].headers["Authorization"] == "Bearer t"

    def test_mcp_json_stdio(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {"mcpServers": {"s1": {"command": "npx", "args": ["-y", "foo"], "env": {"A": "b"}}}}
            )
        )
        servers = load_mcp_json(tmp_path)
        assert servers["s1"].command == "npx"
        assert servers["s1"].args == ["-y", "foo"]
        assert servers["s1"].env == {"A": "b"}

    def test_mcp_json_remote_type_and_enabled(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "on": {"type": "remote", "url": "http://x/mcp", "enabled": True},
                        "off": {"type": "remote", "url": "http://y/mcp", "enabled": False},
                    }
                }
            )
        )
        servers = load_mcp_json(tmp_path)
        assert servers["on"].url == "http://x/mcp"
        assert "off" not in servers

    def test_precedence_cli_over_json(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"s1": {"url": "http://json"}}})
        )
        servers = load_from_args(
            ["s1=http://cli", "s2=http://two"], ["s1=Authorization: Bearer tok"], tmp_path
        )
        assert servers["s1"].url == "http://cli"
        assert servers["s1"].headers["Authorization"] == "Bearer tok"
        assert servers["s2"].url == "http://two"


class TestClientHTTP:
    def test_list_and_call(self, http_server):
        client = MCPClient(name="mock", url=http_server)
        try:
            client.start()
            tools = client.list_tools()
            names = {t.name for t in tools}
            assert {"echo_tool", "add"} <= names

            result = client.call_tool("echo_tool", {"text": "hi"})
            blocks = [b for b in result.content if b.type == "text"]
            assert any("echo: hi" in b.text for b in blocks)
        finally:
            client.close()

    def test_headers_supported(self, http_server):
        client = MCPClient(
            name="mock", url=http_server, headers={"X-Test": "1"}
        )
        try:
            client.start()
            assert client.list_tools()
        finally:
            client.close()


class TestClientStdio:
    def test_list_and_call(self):
        client = MCPClient(name="mock-stdio", command=sys.executable, args=[str(MOCK_SERVER)])
        try:
            client.start()
            tools = client.list_tools()
            names = {t.name for t in tools}
            assert {"echo_tool", "add"} <= names

            result = client.call_tool("add", {"a": 2, "b": 3})
            blocks = [b for b in result.content if b.type == "text"]
            assert any("5" in b.text for b in blocks)
        finally:
            client.close()


class TestManager:
    def test_function_definitions_and_dispatch(self, http_server):
        mgr = MCPManager(output_limit=100, max_roundtrips=2, timeout=30)
        mgr.add_config(MCPServerConfig(name="mock", url=http_server))
        mgr.start()
        try:
            funcs = mgr.function_definitions()
            names = {f["name"] for f in funcs}
            assert "echo_tool" in names

            result = mgr.dispatch("echo_tool", {"text": "hello"})
            assert result["is_error"] is False
            assert "echo: hello" in result["text"]
        finally:
            mgr.shutdown()

    def test_unknown_tool(self, http_server):
        mgr = MCPManager()
        mgr.add_config(MCPServerConfig(name="mock", url=http_server))
        mgr.start()
        try:
            result = mgr.dispatch("nope", {})
            assert result["is_error"] is True
        finally:
            mgr.shutdown()

    def test_truncation(self, http_server):
        mgr = MCPManager(output_limit=10)
        mgr.add_config(MCPServerConfig(name="mock", url=http_server))
        mgr.start()
        try:
            result = mgr.dispatch("echo_tool", {"text": "this is way too long"})
            assert len(result["text"]) <= 10
        finally:
            mgr.shutdown()

    def test_remove_server(self, http_server):
        mgr = MCPManager()
        mgr.add_config(MCPServerConfig(name="mock", url=http_server))
        mgr.start()
        assert mgr.is_mcp_tool("echo_tool")
        mgr.remove_server("mock")
        assert not mgr.is_mcp_tool("echo_tool")


class TestMainWiring:
    def test_main_attaches_manager(self, http_server, tmp_path, monkeypatch):
        from aider.main import main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AIDER_ANALYTICS", "false")

        coder = main(
            argv=[
                "--mcp-server",
                f"mock={http_server}",
                "--yes-always",
            ],
            return_coder=True,
        )
        try:
            mgr = coder.mcp_manager
            assert mgr is not None
            names = {f["name"] for f in mgr.function_definitions()}
            assert "echo_tool" in names
            assert any(f.get("name") == "echo_tool" for f in coder.functions)
        finally:
            coder.mcp_manager.shutdown()


class TestMCPCommands:
    def make_commands(self, http_server, tmp_path, monkeypatch):
        import os

        from aider.coders import Coder
        from aider.commands import Commands
        from aider.io import InputOutput
        from aider.models import Model

        monkeypatch.chdir(tmp_path)
        io = InputOutput(pretty=False, fancy_input=False, yes=True)
        coder = Coder.create(Model("gpt-3.5-turbo"), None, io)

        mgr = MCPManager(io=io)
        mgr.add_config(MCPServerConfig(name="mock", url=http_server))
        mgr.start()
        coder.mcp_manager = mgr
        coder.functions = list(mgr.function_definitions())
        return Commands(io, coder), coder, mgr

    def test_subcoder_with_nonfunc_edit_format(self, http_server, tmp_path, monkeypatch):
        from aider.coders import Coder
        from aider.io import InputOutput
        from aider.models import Model

        monkeypatch.chdir(tmp_path)
        io = InputOutput(pretty=False, fancy_input=False, yes=True)
        coder = Coder.create(Model("gpt-3.5-turbo"), None, io)

        mgr = MCPManager(io=io)
        mgr.add_config(MCPServerConfig(name="mock", url=http_server))
        mgr.start()
        coder.mcp_manager = mgr
        coder.functions = list(mgr.function_definitions())

        try:
            sub = Coder.create(
                coder.main_model, edit_format="whole", io=io, from_coder=coder
            )
            assert sub.mcp_manager is mgr
            names = {f["name"] for f in sub.functions or []}
            assert "echo_tool" in names
        finally:
            coder.mcp_manager.shutdown()

    def test_mcp_status(self, http_server, tmp_path, monkeypatch):
        commands, coder, _ = self.make_commands(http_server, tmp_path, monkeypatch)
        with mock.patch.object(commands.io, "tool_output") as out:
            commands.cmd_mcp("")
        lines = [str(a[0]) for a in out.call_args_list]
        assert any("mock" in line for line in lines)
        assert any("Total tools available: 2" in line for line in lines)

    def test_mcp_list(self, http_server, tmp_path, monkeypatch):
        commands, coder, _ = self.make_commands(http_server, tmp_path, monkeypatch)
        with mock.patch.object(commands.io, "tool_output") as out:
            commands.cmd_mcp("list")
        lines = [str(a[0]) for a in out.call_args_list]
        assert any("echo_tool" in line for line in lines)
        assert any("add" in line for line in lines)

    def test_mcp_exec_injects_result(self, http_server, tmp_path, monkeypatch):
        commands, coder, _ = self.make_commands(http_server, tmp_path, monkeypatch)
        commands.cmd_mcp('exec echo_tool {"text": "hi there"}')
        assert any(
            msg.get("role") == "user" and "[MCP tool 'echo_tool' result]" in msg.get("content", "")
            for msg in coder.cur_messages
        )

    def test_mcp_exec_unknown_tool(self, http_server, tmp_path, monkeypatch):
        commands, coder, _ = self.make_commands(http_server, tmp_path, monkeypatch)
        with mock.patch.object(commands.io, "tool_error") as err:
            commands.cmd_mcp("exec nope_tool")
        assert any("nope_tool" in str(a[0]) for a in err.call_args_list)

    def test_mcp_remove(self, http_server, tmp_path, monkeypatch):
        commands, coder, mgr = self.make_commands(http_server, tmp_path, monkeypatch)
        with mock.patch.object(commands.io, "tool_output"):
            commands.cmd_mcp("remove mock")
        assert not mgr.is_mcp_tool("echo_tool")

    def test_mcp_no_manager(self, tmp_path, monkeypatch):
        import os

        from aider.coders import Coder
        from aider.commands import Commands
        from aider.io import InputOutput
        from aider.models import Model

        monkeypatch.chdir(tmp_path)
        io = InputOutput(pretty=False, fancy_input=False, yes=True)
        coder = Coder.create(Model("gpt-3.5-turbo"), None, io)
        commands = Commands(io, coder)
        with mock.patch.object(commands.io, "tool_error") as err:
            commands.cmd_mcp("list")
        assert any("No MCP servers configured" in str(a[0]) for a in err.call_args_list)
