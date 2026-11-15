# MCP client

`deeppyro.mcp` is a Model Context Protocol **client** aligned with the DSH
`dsh-mcp-client` + `dsh-mcp-resources` packages: it connects to the configured
servers, negotiates the protocol, bridges their tools into the ordinary tool
registry, publishes server instructions into the prompt, and exposes the three
shared resource tools. The bundled `mcp` plugin
(`deeppyro.builtin.mcp:McpPlugin`) owns the lifecycle.

It never starts an MCP *server*, never performs I/O at import time, and adds no
third-party dependency (`stdio` uses `asyncio` subprocesses, `streamable-http`
uses `httpx`).

## Configuration

```toml
[[mcp.servers]]
serverName = "github"                  # also accepted as `name`
transport = "stdio"                    # "stdio" | "streamable-http" (alias: "http")
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
cwd = ""                               # stdio working directory
env = { GITHUB_TOKEN = "..." }         # merged over a scrubbed environment
enabled = true
toolCallTimeoutMs = 60000
maxInstructionBytes = 32768
failOnStartupError = false

[mcp.servers.reconnect]
enabled = true
initialDelayMs = 500
maxDelayMs = 30000
maxAttempts = 10

[[mcp.servers]]
serverName = "remote"
transport = "streamable-http"
url = "https://mcp.example.com/mcp"
headers = { Authorization = "Bearer $TOKEN" }
```

| Field | Default | Meaning |
|---|---|---|
| `serverName` (`name`) | required | `[A-Za-z0-9_-]{1,32}`; unique across rows; the tool namespace |
| `transport` | `stdio` | `stdio` or `streamable-http` (`http` is accepted as an alias) |
| `command` / `args` / `cwd` / `env` | — | stdio only; `env` is merged over the scrubbed parent environment |
| `url` / `headers` | — | `streamable-http` only |
| `toolCallTimeoutMs` | `60000` | per `tools/call` and per resource request |
| `maxInstructionBytes` | `32768` | UTF-8 budget for server instructions; larger instructions are dropped |
| `failOnStartupError` | `false` | `true` aborts plugin activation instead of degrading |
| `reconnect.*` | on, 500 ms, 30 s, 10 | backoff policy after a connection is lost |

Two rows with the same `serverName` fail loudly (`McpConfigError`) rather than
silently keeping one. Unknown keys are allowed and ignored (forward
compatibility).

## Transports

### stdio

Newline-delimited JSON over the child process' stdin/stdout, spawned with
`asyncio.create_subprocess_exec(cwd=...)`. The child environment is built by
`scrubbed_env`: every parent variable whose name matches `KEY|PASSWORD|SECRET|
TOKEN` (case-insensitive) and every `DSH_*`/`DEEPPYRO_*`/`DPYRO_*` name is
removed before `[[mcp.servers]].env` is merged on top — configured values still
win. stderr is drained line by line into the `deeppyro.mcp.<server>` logger. If
the child exits, pending requests fail with `McpTransportError`; `close()`
closes stdin, sends `terminate`, waits five seconds, then kills.

### streamable-http

Each JSON-RPC message is a `POST` to `url` with
`Accept: application/json, text/event-stream`. JSON and SSE bodies are both
accepted; `mcp-session-id` is remembered and echoed. `202`/`204` answers to a
request are errors, HTTP >= 400 becomes `McpTransportError` with a truncated
body.

## Handshake

1. `initialize` with `2025-06-18`; on a JSON-RPC error it is retried once with
   `2024-11-05`. Whatever version the server returns is recorded.
2. `notifications/initialized`.
3. `tools/list`, following `nextCursor` (at most 100 pages).
4. `instructions` from the `initialize` result are kept when they fit
   `maxInstructionBytes`.

A server whose handshake fails is logged and skipped unless
`failOnStartupError = true`.

## Tool naming

`mcp__<serverName>__<rawName>`. Server and tool parts are sanitised
independently (`[^A-Za-z0-9_-]` → `_`); when the *tool* part was rewritten the
public name gains a `_<12 hex>` suffix derived from `sha256(server \0 raw)`, so
two distinct identities can never collapse onto one name:

```text
("web", "read_page")  -> mcp__web__read_page
("web", "read page")  -> mcp__web__read_page_4a1c9f0d2b73
```

`tools/call` always sends the raw name. A server that lists the same tool twice
has its whole generation rejected (the previous generation is kept) — a server
never ends up with a partial tool set. Input schemas are forwarded verbatim when
they are object schemas with `properties`; adapters are `parallel_safe = False`.

## Results

* `text` blocks are concatenated; `image`/`audio`/`resource` blocks become
  one-line placeholders such as `[image: image/png]`.
* Image blocks (png/jpeg/webp/gif) are persisted through the `attachments`
  service when it is available, and surface to the model as an image
  follow-up message; if the store rejects them the placeholder text remains.
* `isError = true`, JSON-RPC errors and transport failures become `ToolError`,
  so the model sees a failed tool call rather than a crashed turn.

## Server instructions in the prompt

When at least one server is configured the plugin registers the prompt section
`mcp` (order 40). It renders:

```text
## MCP

Use list_mcp_resources, list_mcp_resource_templates, or read_mcp_resource
with one of these names as the server argument: ["github", "docs"].

### docs

<that server's instructions>

```

Instructions are dropped (with a warning) when they exceed
`maxInstructionBytes`; a server that returns none contributes no paragraph.

## Shared resource tools

With any configured server the plugin registers three tools, each requiring an
explicit `server` argument (mirroring `dsh-mcp-resources`):

| Tool | Arguments | Result |
|---|---|---|
| `list_mcp_resources` | `server`, optional `cursor` | one page of `resources/list`, rendered as `MCP server: <name>` + JSON |
| `list_mcp_resource_templates` | `server`, optional `cursor` | one page of `resources/templates/list` |
| `read_mcp_resource` | `server`, `uri` | `MCP server: <name>` + JSON with every string `blob` replaced by `[binary resource: N base64 characters; available to programmatic callers]` |

Omit `cursor` for the first page and echo `nextCursor` back for the next one.
A server that does not declare the `resources` capability answers an empty page
without contacting it. Unknown or down servers fail the call (never dispatch to
a wrong server). Resource text enters history only when the model asks.

## Lifecycle and recovery

`McpService.start()` connects every enabled server concurrently and returns one
`McpServerStatus` per enabled row (`ok`, `error`, `tools`, `instructions`,
`resources`, `reconnecting`). `status()` is the read-only view for the TUI/CLI;
`reload()` drops all sessions and connects again.

A failed `tools/call` or resource request marks the connection as lost and — if
`reconnect.enabled` — starts one supervisor task that retries with exponential
backoff (`initialDelayMs * 2^(n-1)`, capped by `maxDelayMs`). Each interruption
shares one attempt budget: after `maxAttempts` consecutive failures the server's
tools are unregistered and the paragraph disappears; a connection that stayed up
for at least `maxDelayMs` resets the budget. During an outage the last known
tools stay listed and their calls fail.

Connecting emits `mcp/connected` with `{"server", "tools"}`; disconnecting (or
giving up) emits `mcp/disconnected`. Both are ordinary bus events produced by
`mcp.service`.

## Non-goals

MCP prompts/templates-as-prompts, sampling, elicitation, resource *subscriptions*
and update notifications, and task-based execution tools are not bridged — same
limits as DSH. Resources are read on demand only; binary payloads stay textual
for the model.

## Testing

`tests/mcp/` covers framing, both transports (a real `sys.executable -c` fake
server for stdio, `respx` for HTTP), the handshake, version fallback, pagination,
identity naming, environment scrubbing, instructions, resource tools, backoff,
the attempt budget, and the service lifecycle. No test performs real network I/O
or spawns a real MCP server.
