# MCP client

`deeppyro.mcp` is a Model Context Protocol **client**: it connects to the MCP
servers named in the user config, performs the `initialize` handshake, lists
their tools and bridges every tool into the ordinary deeppyro tool registry.
The bundled `mcp` plugin (`deeppyro.builtin.mcp:McpPlugin`) owns the lifecycle.

Resources, prompts, sampling and elicitation are deliberate non-goals: v1 is a
tool bridge only. Nothing here starts an MCP *server*, and nothing here talks to
the network at import time or during plugin construction.

## Configuration

Servers live in the user config under `[[mcp.servers]]`:

```toml
[[mcp.servers]]
name = "filesystem"
transport = "stdio"          # "stdio" (default) or "http"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
env = { LOG_LEVEL = "warn" }
enabled = true
startup_timeout = 20.0
call_timeout = 60.0

[[mcp.servers]]
name = "remote"
transport = "http"
url = "https://mcp.example.com/mcp"
headers = { Authorization = "Bearer $TOKEN" }
```

* `name` is required and unique; it is part of every tool name.
* `transport = "stdio"` requires `command`; `args` and `env` are optional and
  `env` is merged over the inherited environment.
* `transport = "http"` requires `url`; `headers` are sent on every request.
* `enabled = false` keeps the row but never connects.
* `startup_timeout` bounds the process spawn plus `initialize`; `call_timeout`
  bounds `tools/call` and `tools/list`.
* Unknown keys are allowed (forward compatibility) and ignored.

## Transports

### stdio

Newline-delimited JSON over the child process' stdin/stdout, spawned with
`asyncio.create_subprocess_exec`. stderr is drained line by line into the
`deeppyro.mcp.<server>` logger so a chatty server can never block on a full
pipe. If the child exits, every pending request fails with
`McpTransportError` instead of hanging. `close()` closes stdin, sends
`terminate`, waits up to five seconds and then kills the process.

### http (streamable HTTP)

Each JSON-RPC message is a `POST` to `url` with
`Accept: application/json, text/event-stream`. A response body may be plain
JSON or a server-sent-event stream; both are accepted. The `Mcp-Session-Id`
response header is remembered and echoed on later requests. Notifications are
expected to answer `202` (a `202` to a request is an error). HTTP status codes
at or above 400 become `McpTransportError` with a truncated body.

## Handshake

1. `initialize` with `protocolVersion = "2025-06-18"`, `capabilities = {}` and
   `clientInfo = {"name": "deeppyro", "version": <package version>}`.
2. If the server answers with a JSON-RPC error, `initialize` is retried once
   with `"2024-11-05"`; whatever `protocolVersion` the server returns is
   recorded as `McpClient.protocol_version`.
3. `notifications/initialized` (no id, no response).
4. `tools/list`, following `nextCursor` until it is absent (at most 100 pages).

A server whose handshake fails is logged and skipped; it never fails boot.

## Tool naming

`mcp__{server}__{tool}` with every character outside `[A-Za-z0-9_-]` replaced by
an underscore, independently for the server and the tool part. For example
server `my server` and tool `do.thing/x` become
`mcp__my_server__do_thing_x`. If two servers sanitise to the same prefix and
expose the same tool, the later adapter is dropped with a warning so the tool
registry stays free of duplicates.

The adapter forwards the remote `inputSchema` verbatim when it is an object
schema with `properties`; otherwise it exposes an empty object schema. Remote
tools are declared `parallel_safe = False`, so two calls of the same MCP tool
never overlap.

## Results and failures

`tools/call` results keep `content` and `isError` as the server sent them:

* `type = "text"` items are concatenated with newlines.
* `image`/`audio`/`resource` items become one-line placeholders such as
  `[image: image/png]`; binary content is not downloaded in v1.
* `isError = true` raises `ToolError` with the flattened text, which the tool
  pipeline turns into an error tool result.
* JSON-RPC errors and transport failures also become `ToolError`, so the model
  sees a normal failed tool call rather than a crashed turn.

## Lifecycle and events

`McpService.start()` connects every enabled server concurrently and returns one
`McpServerStatus` per configured server (`ok`, `error`, `tools`).
`McpService.status()` is the read-only view used by the TUI and CLI, and
`reload()` drops all sessions and connects again.

Connecting emits `mcp/connected` with `{"server": <name>, "tools": [<tool names>]}`;
closing emits `mcp/disconnected` with the same shape (an empty tool list). Both
are ordinary bus events (`mcp.service` is the producer); a missing bus is
ignored, never fatal.

## Testing

`tests/mcp/` covers the framing helpers, both transports (a real
`sys.executable -c` fake server for stdio, `respx` for HTTP), the handshake,
version fallback, pagination, adapter behaviour and the service lifecycle. No
test performs real network I/O or spawns a real MCP server.
