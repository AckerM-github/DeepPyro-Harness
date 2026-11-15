# dpyro-im: the IM shape for deeppyro

`dpyro-im` lives out of tree (`D:\Code\dpyro-im`) and installs as an ordinary
plugin package:

```
dpyro plugin add D:\Code\dpyro-im --no-deps
```

## Why a plugin and not a core feature

The transport stacks (Discord Gateway v10, the QQ bot websocket protocol and
its QR login) have nothing to do with the harness. Keeping them in a package
means a frozen exe can still load them, and the dependency set (`websockets`,
`cryptography`, `segno`) never leaks into the core environment.

## Runtime shape

- one `AgentSession` per conversation key `(platform, kind, id)`, each with
  its own serial queue and its own `sandbox.gate(..., session=...)`, so an
  approval prompt lands in the chat that caused it
- routes persist in `~/.dpro/im/routes.json`; different chats never share a
  session, including two private chats with different users
- configuration lives in `~/.dpro/im/config.toml` (0600)

## Deliberate decisions

1. `KernelBackend` boots one runtime per `(workspace, preset)` instead of
   borrowing the TUI kernel. Sharing it would mean two frontends driving the
   same in-memory session; isolation costs one boot and removes a class of
   bugs where one frontend blocks the other.
2. Approvals and questions are text-only on both platforms. QQ has no buttons
   and this Discord adapter deliberately has no interactions endpoint, so both
   render a keyword or numbered prompt and match the next message in that
   conversation.
3. Adapters never chunk text on their own: `Stream.finish()` hands over the
   final answer and the adapter applies its own split (1900 characters for
   Discord, 4500 for QQ Markdown, plus the passive reply budget).
4. Credentials are only exercised by unit tests so far. A real Discord token
   or a QQ scan is required before either channel can be called production
   ready.
