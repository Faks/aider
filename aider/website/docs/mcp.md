# Model Context Protocol (MCP) support

Aider can connect to [MCP](https://modelcontextprotocol.io) servers and use
their tools, both automatically (the model can call them while answering) and
manually (via the `/mcp` command in the terminal UI, or the MCP panel in the
browser UI).

MCP servers expose tools over two transport types, both supported:

- **Streamable HTTP** — a URL such as `http://host:port/mcp`, optionally with
  custom headers (e.g. an `Authorization: Bearer ...` token).
- **Stdio** — a local command (plus args/env) that Aider runs as a subprocess.

## Configuration

MCP servers can be configured in four ways. If the same server name appears in
more than one place, the later entry wins.

| Source | Precedence |
| --- | --- |
| `.aider.conf.yml` (global or project) | lowest |
| `.mcp.json` (home, then git root, then cwd) | |
| CLI flags (`--mcp-server`, `--mcp-header`) | |
| `/mcp add` at runtime + `/mcp save` | highest |

### `.aider.conf.yml`

```yaml
mcp-server: metamcp=http://127.0.0.1:9090/mcp
mcp-header: 'metamcp=Authorization: Bearer sk_...'
mcp-tool-output-limit: 16000
mcp-max-roundtrips: 5
```

### `.mcp.json` (project-based)

The community-standard `.mcp.json` file is searched in the home directory, the
git root, then the current directory. This is the recommended way to share
servers with your team or other tools.

```json
{
  "mcpServers": {
    "metamcp": {
      "type": "http",
      "url": "http://127.0.0.1:9090/mcp",
      "headers": {
        "Authorization": "Bearer sk_..."
      }
    },
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "env": {}
    }
  }
}
```

### CLI flags

```bash
aider --mcp-server metamcp=http://127.0.0.1:9090/mcp \
      --mcp-header 'metamcp=Authorization: Bearer sk_...'
```

### Environment variable

```bash
export AIDER_MCP_SERVER="metamcp=http://127.0.0.1:9090/mcp"
aider
```

## Using MCP tools automatically

When at least one MCP server is configured and connected, its tools are added
to the model's function list automatically. The model may call them while
answering; results are injected back into the conversation so it can reason
over them.

Because local models can occasionally loop on tool calls, Aider stops after
`mcp-max-roundtrips` tool-call roundtrips per message (default 5). Tool output
is truncated to `mcp-tool-output-limit` characters (default 16000) to protect
your context window.

## The `/mcp` command (terminal UI)

- `/mcp` — show connected servers and total tool count.
- `/mcp list` — list all available tools.
- `/mcp exec <tool> [json args]` — run a tool and inject the result into the
  chat. With no JSON given, you are prompted for arguments.
- `/mcp add` — interactive wizard to add a server (http or stdio), with a live
  connection test.
- `/mcp remove <name>` — disconnect a server for this session.
- `/mcp save` — persist the session's servers to `.mcp.json`.
- `/mcp reload` — re-read config files and reconnect.
- `/mcp history` — show the most recent MCP tool results.

## Browser UI

When a server is connected, the sidebar shows an **MCP Tools** panel where you
can pick a tool, fill in its parameters, and run it. Results are injected into
the chat.

## Security notes

- Headers such as `Authorization` tokens are stored in plain text in your
  config files. If you commit `.mcp.json` to a repo, avoid putting real tokens
  in it.
- MCP tools can perform arbitrary actions (run commands, read/write files).
  Only connect to servers you trust.

## Troubleshooting

- **"No MCP servers configured"** — add one via any of the config sources
  above, then restart Aider.
- **"Unable to connect to MCP server"** — check the URL/port, headers, and that
  the server is reachable. Stdio servers need the command to be installed.
- **Model never calls tools** — some local models emit tool calls unreliably.
  Try a stronger model, or run tools manually with `/mcp exec`.
