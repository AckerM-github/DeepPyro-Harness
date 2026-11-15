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

## Operating rules

Three rules that cost real debugging time, recorded so they are not rediscovered.

1. **One dpyro_im serve per bot token.** Discord allows a single gateway session per
   bot; a second process takes the session over, the first receives op 9 and closes with
   4000 Invalid session, and the account looks offline from the Discord client. Before
   starting a gateway, check for another python -m dpyro_im serve on the same machine.
2. **Discord needs the MESSAGE_CONTENT privileged intent.** GET /applications/@me
   reports it as lags & (1 << 18); while it is off the gateway still delivers
   MESSAGE_CREATE, but content is empty, so the adapter drops every message as
   text-less. The bot page in the Developer Portal has the switch.
3. **IM session titles keep the platform prefix and the fallback writer owns them.**
   dpyro-im records QQ C2C · 6A14 · 你好 style titles on the first turn, so the
   session-title gist plugin sees an existing title and (by design) never overwrites an
   IM session. Gists are a TUI affordance; IM titles carry the conversation identity.
