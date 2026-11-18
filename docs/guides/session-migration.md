# 会话格式迁移指南（v4 ↔ native schema 1）

> 对应 0.2.0 已知问题 KI-002。0.2.0 是 native 过渡版：native 会话写
> `<session-id>.jsonl`，v4 会话写 `session.v4.jsonl`，两种格式互不读取。
> `dpyro migrate` 提供一次性双向迁移，用于升级后继续使用旧会话，或把 native
> 会话回滚成 0.1.x 可读的 v4 日志。

## 1. 命令

```bash
# v4 -> native（全部会话，默认先备份目标）
dpyro migrate --to native --all

# 只迁移一个会话
dpyro migrate --to native --session session-11111111-1111-4111-8111-111111111111

# 预览：只做格式检测与转换（含读回校验），不写任何文件
dpyro migrate --to native --all --dry-run

# 覆盖已有且内容不同的目标（默认会拒绝并记为 skipped）
dpyro migrate --to v4 --all --yes

# 关闭覆盖前备份（默认开启）
dpyro migrate --to v4 --all --yes --no-backup

# 机器可读输出
dpyro migrate --to native --all --json
```

默认根目录与运行时一致：

- native 根：`paths.sessions_dir()`，即 `DEEPPYRO_SESSION_DIR` 或 `~/.dpro/sessions`；
- v4 根：`config.sessionDir`，未配置时与 native 根相同；
- `--native-dir` / `--v4-dir` 可显式覆盖（运维与测试用）。

`migrate` 在 TUI 参数解析之前分发（`src/deeppyro/tui/main.py`），不启动 runtime、
不加载模型；因此可以在无 provider 配置的机器上运行。

## 2. 退出码与汇总

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部成功：converted / planned / up-to-date 均算成功，无 skipped、failed |
| `2` | 部分成功：至少一个文件成功，但另有文件被跳过（损坏、未知格式、目标冲突）或失败 |
| `1` | 失败或无可迁移项：没有任何成功项，或 `--session` 指定的会话不存在 |

每个文件都会出现在汇总里（人类可读表格或 `--json` 的 `entries`），
跳过/失败一定带 `reason`。损坏或格式未知的文件不会被猜测处理，只会被跳过。

## 3. 安全保证

- **原子写**：先写同目录临时文件（`flush` + `fsync`），再用 `os.replace` 原子替换；
  中途失败不会留下半截目标文件。
- **写前读回验证**：转换结果先写到临时目录并按目标格式重新解析——
  native 走 `NativeCodec.read` + `fold`，v4 走 `read_session_file` + `derive_messages`；
  校验失败记为 `failed`，不落盘。
- **默认备份**：目标已存在且将被覆盖时，先复制为 `<name>.bak`、`<name>.bak.1`……
  （取第一个不存在的后缀）。
- **幂等**：目标字节与本次转换结果一致时记为 `up-to-date`，不重写文件（mtime 不变）。
- **冲突需显式确认**：目标存在且内容不同时，默认跳过并提示 `pass --yes`；
  只有传 `--yes` 才会覆盖（并默认备份）。
- **不删源文件**：迁移只新增/覆盖目标文件，源日志保留，可人工核对后删除。
- **dry-run 不落盘**：`--dry-run` 只做检测与转换，输出状态 `planned`。

## 4. v4 → native：保留什么

native schema 1 是事实日志（§AI 原生要求），一次迁移生成：

- 会话头：`id`（v4 id 已符合 `session-<uuid>` 时沿用，否则按内容生成确定性 UUID，
  保证同一输入重复迁移得到同一 id）、`created_at`（原 `createdAt`）、`cwd`、
  `preset`（原 `agentPreset`，非法时回落 `standard`）、`parent_id`、
  `origin`、`delegation_depth`；
- 对话内容：user / assistant / tool / system / developer 消息文本与
  reasoning；assistant 消息内嵌的 tool-call 结构；`tool/call` 与
  `tool/result` 的配对（含 `sourceEventSeqs` 引用关系）；
- 时间戳：消息记录优先使用原 `time`，并保证 native 要求的单调不减；
- turn/step 边界与 `turn/end` 的 reason（completed / aborted / error /
  max-steps / max-tokens / blocked 映射到 native reason）；
- 少量状态事件：`session/title`、`model/selection`、`sandbox/mode`、
  `working-directory/change`；
- 被 surface replace/retire 遮蔽的旧消息不进入 native 日志，迁移使用 fold 后
  可见的对话内容（结果与 v4 reader 一致）。

为保证 native 日志本身合法，迁移会在两种情况下做修复并记录 warning：

1. `completed` turn 里有 assistant tool-call 缺少对应 tool result 时，补一条
   `source=repair`、`is_error=true` 的 synthetic tool result（文本
   “tool result was never recorded: ...”），而不是伪造成功结果；
2. 日志被截断（trimmed）导致 tool result 找不到所属 call 时，补最小 assistant
   消息 + `tool.call` 再写 result。

**不保留（明确丢弃）**：图片内容（native schema 1 的 ImagePart 需要
`media_type`，v4 图片只有附件引用）、approval / goal / plan / todo /
deliverable / compaction / job / team / subagent / hook / request / usage
等状态与遥测事件，以及 v4 原始 message id（native 使用新的 `msg_…` id）。
原生格式只承诺事实与对话，不承诺 UI 状态在回滚后完整还原。

## 5. native → v4：回滚边界

回滚生成 v4 reader 可读的最小合法记录，**只保证两件事**：

1. `dpyro` 0.1.x 的 v4 reader 能完整读出日志（header + 连续 seq + 合法事件）；
2. 对话内容（system / user / assistant / tool 文本、assistant tool-call 结构、
   tool result 配对、turn/step 边界）与 native 一致。

它不是逐字节还原：header 之外的状态事件、native 的 run.* / compaction /
approval / plugin 事件会丢弃；native message id 直接作为 v4 message id；
v4 `tool/result` 通过 `sourceEventSeqs` 指向对应的 `tool/call` 记录。
需要逐字节保留的场景请直接备份 native 日志，不要依赖回滚。

## 6. 作为插件复用

迁移核心是纯函数模块 `src/deeppyro/migrate.py`（`detect_format`、
`v4_to_native`、`native_to_v4`、`run_migration`），CLI 只负责参数与 IO。
kernel 中另外提供 `session_migrate` Service：

```python
from deeppyro.migrate import SessionFormat

service = ctx.require(SessionMigratePlugin)  # provide="session_migrate"
report = service.migrate(to=SessionFormat.NATIVE, all=True, dry_run=True)
```

插件实现位于 `src/deeppyro/session/migrate_service.py`，通过 `inject=["config"]`
读取会话根目录；启用需要在 `src/deeppyro/bundles/core/bundle.toml` 注册：

```toml
[[plugins]]
id = "session-migrate"
plugin = "deeppyro.session.migrate_service:SessionMigratePlugin"
```

## 7. 测试与 fixtures

- `tests/session/test_migrate.py` 覆盖 round-trip 文本等价、幂等、`--dry-run`
  不落盘、损坏/未知输入跳过、`--session` 选择、备份与 `--no-backup`、
  CLI 退出码/JSON、Service 挂载、TUI 分发。
- `tests/fixtures/synthetic/` 提交最小合成 v4 fixture（3 个会话 + 42 个
  by-type 样本 + `manifest.json`，总量 <60KB）。真实隐私 fixtures 存在时
  `tests/session/support.py` 优先使用真实数据；缺失时（干净 checkout / CI）
  自动回退到合成数据，核心 reader 用例不再整模块 skip（KI-030）。
