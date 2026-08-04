"""A single MCP server connection.

Runs the MCP SDK's async session (streamable HTTP or stdio) on a dedicated
background thread and exposes blocking ``list_tools`` / ``call_tool``
methods that are safe to call from Aider's sync code.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Optional

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool


class MCPClient:
    def __init__(
        self,
        name: str,
        url: Optional[str] = None,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 60,
    ):
        self.name = name
        self.url = url
        self.command = command
        self.args = args or []
        self.env = env
        self.headers = headers or {}
        self.timeout = timeout

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop_evt: Optional[asyncio.Event] = None
        self._session: Optional[ClientSession] = None
        self._session_task: Optional[asyncio.Task] = None
        self._http_client = None
        self._tools: Optional[list[Tool]] = None
        self._session_error: Optional[Exception] = None
        self._closed = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._session is not None

    def start(self) -> None:
        """Connect to the server on a background thread (blocking until ready)."""
        if self._thread and self._thread.is_alive():
            return
        if self._session is not None:
            return

        self._ready.clear()
        self._closed.clear()
        self._session = None
        self._thread = threading.Thread(
            target=self._thread_main, name=f"mcp-{self.name}", daemon=True
        )
        self._thread.start()

        if not self._ready.wait(timeout=self.timeout):
            raise TimeoutError(f"Timed out connecting to MCP server '{self.name}'")

        # Launch the session-open coroutine on the background loop
        self._loop.call_soon_threadsafe(self._start_session_task)

        if not self._wait_for_session():
            raise RuntimeError(f"Failed to initialize MCP server '{self.name}'")

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()

        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            if self._http_client is not None:
                try:
                    self._loop.run_until_complete(self._http_client.aclose())
                except Exception:
                    pass
            self._loop.close()

    def _wait_for_session(self) -> bool:
        """Block until the session is initialized and tools listed (or timeout)."""
        import time

        for _ in range(int(self.timeout * 10)):
            if self._tools is not None:
                return True
            if self._session_error is not None:
                return False
            time.sleep(0.1)
        return self._tools is not None

    def _submit(self, coro) -> Any:
        """Run an async coroutine on the background event loop, blocking for it."""
        if not self._loop or not self._loop.is_running():
            raise RuntimeError(f"MCP server '{self.name}' is not connected")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=self.timeout)

    async def _open_session(self) -> None:
        self._stop_evt = asyncio.Event()

        if self.url:
            import httpx

            if self.headers:
                self._http_client = httpx.AsyncClient(headers=self.headers)
                async with streamable_http_client(
                    self.url, http_client=self._http_client
                ) as (read, write, _get_sid):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await self._publish_session(session)
            else:
                async with streamable_http_client(self.url) as (read, write, _get_sid):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await self._publish_session(session)
        elif self.command:
            import mcp

            params = mcp.StdioServerParameters(
                command=self.command, args=self.args, env=self.env
            )
            async with mcp.stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await self._publish_session(session)
        else:
            raise ValueError("MCP server must specify either url or command")

    async def _publish_session(self, session: ClientSession) -> None:
        self._session = session
        try:
            result = await session.list_tools()
            self._tools = result.tools if result else []
        except Exception as err:
            self._session_error = err
        finally:
            if self._stop_evt is not None:
                await self._stop_evt.wait()

    def _start_session_task(self) -> None:
        assert self._loop is not None
        self._session_task = self._loop.create_task(self._open_session())
        self._session_task.add_done_callback(self._on_session_done)

    def _on_session_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None and not self._session_error:
            self._session_error = task.exception()

    def list_tools(self) -> list[Tool]:
        """Return the list of tools advertised by the server (blocking)."""
        if self._tools is None:
            if self._session_error is not None:
                raise RuntimeError(f"MCP server '{self.name}': {self._session_error}")
            if not self._wait_for_session():
                raise RuntimeError(f"MCP server '{self.name}' failed to initialize")
        return list(self._tools or [])

    def call_tool(self, tool_name: str, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Execute a tool on the server (blocking) and return a result dict."""
        if not self.is_running:
            raise RuntimeError(f"MCP server '{self.name}' is not connected")

        if self._session_error is not None:
            raise RuntimeError(f"MCP server '{self.name}': {self._session_error}")

        return self._submit(self._session.call_tool(tool_name, arguments or {}))

    def close(self) -> None:
        """Tear down the session and stop the background loop."""
        if self._closed.is_set():
            return
        self._closed.set()

        if self._loop is not None and self._loop.is_running():
            async def _stop():
                if self._stop_evt is not None:
                    self._stop_evt.set()
                if self._session_task is not None and not self._session_task.done():
                    self._session_task.cancel()
                    try:
                        await self._session_task
                    except (asyncio.CancelledError, Exception):
                        pass

            try:
                self._submit(_stop())
            except Exception:
                pass
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread is not None:
            self._thread.join(timeout=self.timeout)
        self._thread = None