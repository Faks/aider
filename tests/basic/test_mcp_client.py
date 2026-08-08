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
            assert coder.mcp_max_roundtrips == 0
            assert mgr.max_roundtrips == 0
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


class TestMCPAutoDispatch:
    """Regression tests for the auto-dispatch loop and tool_calls round-trip.

    The model must be able to emit modern ``tool_calls`` (not legacy
    ``function_call``) and receive results back as ``role="tool"`` messages
    with a matching ``tool_call_id`` - that's what the local llama.cpp router
    expects. These tests use only the mock server (never a real MCP endpoint).
    """

    def make_coder(self, http_server, tmp_path, monkeypatch, stream=False):
        from aider.coders import Coder
        from aider.io import InputOutput
        from aider.models import Model

        monkeypatch.chdir(tmp_path)
        io = InputOutput(pretty=False, fancy_input=False, yes=True)
        coder = Coder.create(Model("gpt-3.5-turbo"), None, io, stream=stream)

        mgr = MCPManager(io=io)
        mgr.add_config(MCPServerConfig(name="mock", url=http_server))
        mgr.start()
        coder.mcp_manager = mgr
        coder.functions = list(mgr.function_definitions())
        return coder, mgr

    def test_streaming_tool_calls_accumulate(self, tmp_path, monkeypatch):
        """Modern ``delta.tool_calls`` chunks must accumulate name/arguments/id."""
        from types import SimpleNamespace

        from aider.coders import Coder
        from aider.io import InputOutput
        from aider.models import Model

        monkeypatch.chdir(tmp_path)
        io = InputOutput(pretty=False, fancy_input=False, yes=True)
        model = Model("gpt-3.5-turbo")
        coder = Coder.create(model, None, io=io, stream=True)

        def make_chunk(tool_calls):
            chunk = mock.MagicMock()
            chunk.choices = [mock.MagicMock()]
            chunk.choices[0].finish_reason = None
            chunk.choices[0].delta = mock.MagicMock()
            chunk.choices[0].delta.content = None
            chunk.choices[0].delta.reasoning_content = None
            chunk.choices[0].delta.reasoning = None
            chunk.choices[0].delta.tool_calls = tool_calls
            return chunk

        chunks = [
            make_chunk(
                [
                    SimpleNamespace(
                        id="call_abc123",
                        function=SimpleNamespace(
                            name="echo_tool", arguments='{"te'
                        ),
                    )
                ]
            ),
            make_chunk(
                [
                    SimpleNamespace(
                        id=None,
                        function=SimpleNamespace(name=None, arguments='xt": "hi"}'),
                    )
                ]
            ),
        ]

        mock_hash = mock.MagicMock()
        mock_hash.hexdigest.return_value = "mock_hash_digest"
        with mock.patch.object(
            model, "send_completion", return_value=(mock_hash, chunks)
        ):
            messages = [{"role": "user", "content": "hello"}]
            list(coder.send(messages))

        assert coder.partial_response_function_call == {
            "name": "echo_tool",
            "arguments": '{"text": "hi"}',
        }
        assert coder.partial_response_tool_call_id == "call_abc123"

    def test_auto_dispatch_emits_modern_tool_calls(self, http_server, tmp_path, monkeypatch):
        """The dispatch loop must round-trip as modern tool_calls + role=tool."""
        coder, mgr = self.make_coder(http_server, tmp_path, monkeypatch, stream=False)

        calls = []

        def fake_send(messages, model=None, functions=None):
            calls.append(messages)
            if len(calls) == 1:
                coder.partial_response_function_call = {
                    "name": "echo_tool",
                    "arguments": '{"text": "hi"}',
                }
                coder.partial_response_tool_call_id = "call_abc123"
            else:
                coder.partial_response_function_call = dict()
                coder.partial_response_tool_call_id = None
                coder.partial_response_content = "I echoed it."
            yield None

        try:
            with mock.patch.object(coder, "send", fake_send):
                coder.run_one("hello", preproc=False)

            assert len(calls) == 2, "second send() is the post-tool round trip"

            roles = [m["role"] for m in coder.cur_messages]
            assert roles.count("tool") == 1
            assert roles.count("assistant") == 2  # tool_calls msg + final content msg

            assistant_msg = next(
                m for m in coder.cur_messages if m.get("tool_calls")
            )
            assert assistant_msg["content"] is None
            assert assistant_msg["tool_calls"] == [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "echo_tool",
                        "arguments": '{"text": "hi"}',
                    },
                }
            ]
            # No legacy function_call assistant message for MCP tools
            assert not any(
                m.get("role") == "assistant" and "function_call" in m
                for m in coder.cur_messages
            )

            tool_msg = next(m for m in coder.cur_messages if m["role"] == "tool")
            assert tool_msg["tool_call_id"] == "call_abc123"
            assert tool_msg["content"].startswith("[MCP tool 'echo_tool' result]")
        finally:
            mgr.shutdown()
