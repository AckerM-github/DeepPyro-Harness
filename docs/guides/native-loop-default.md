# headless native loop 默认化与 v4 回退（KI-001 / KI-010）

> 本次默认切换落在 0.2.0 代码线：headless 默认走 native loop（schema 1 会话），
> `--legacy-v4` 回退旧 v4 路径，`--native-loop` 降为弃用别名。本文同时记录
> TUI 迁移状态（KI-001 仍为 partial）与 `core.wire` 的弃用边界（KI-010）。
> `docs/release/0.2.0-known-issues.md` 中 KI-001 / KI-010 的最终状态由发布文档
> 维护者在合流后统一更新，本文不替代总表。

## 1. 行为矩阵

| 调用方式 | 生效后端 | 会话文件 | 说明 |
| --- | --- | --- | --- |
| `dpyro --headless "..."`（无开关、无 env） | native | `<session-id>.jsonl`（schema 1） | 0.2.0 起默认 |
| `dpyro --headless --legacy-v4 "..."` | v4 | `session.v4.jsonl` | 显式回退；优先于 `DEEPPYRO_NATIVE_LOOP` |
| `dpyro --headless --native-loop "..."` | native | `<session-id>.jsonl` | 弃用别名，stderr 警告 + `DeprecationWarning` |
| `DEEPPYRO_NATIVE_LOOP=1`（`1/true/yes/on`） | native | `<session-id>.jsonl` | 非 CLI 开关仍受支持 |
| `DEEPPYRO_NATIVE_LOOP=0`（`0/false/no/off`） | v4 | `session.v4.jsonl` | 显式回退的另一种写法 |
| env 为其他无法识别的值 | native | `<session-id>.jsonl` | 宽松回退到新默认，不报错 |
| TUI（无 prompt / 无 `--headless`） | v4 | `session.v4.jsonl` | 本次不改；开关只影响 headless |

位置参数 prompt 与 `--headless` 等价（`headless = bool(args.headless or args.prompt)`，
`src/deeppyro/tui/main.py:161`），因此 `dpyro "总结这个仓库"` 也走默认后的 native 路径。
`--legacy-v4` 与 `--native-loop` 互斥：argparse 在参数解析阶段直接拒绝同时出现（退出码 2）。

## 2. 优先级与实现

`src/deeppyro/headless.py:129-146` 的 `native_loop_enabled(flag)` 是唯一判定入口，三级优先级：

1. `flag is not None` 时以 flag 为准：`False` 钉住 v4（`--legacy-v4` 与库调用者），
   `True` 钉住 native；
2. 否则读 `DEEPPYRO_NATIVE_LOOP`（常量在 `src/deeppyro/headless.py:102-104`）：
   真值集合 `1/true/yes/on` 选 native，假值集合 `0/false/no/off` 选 v4；
3. 未设置或无法识别时返回 native（`return value not in NATIVE_LOOP_FALSY`）。

调用链：

- `src/deeppyro/headless.py:807-859` 的 `run_once(..., native_loop: bool | None = None)`：
  参数在 `:818`，语义写在 `:822-830`，`:851` 调用 `native_loop_enabled(native_loop)`；
- `src/deeppyro/headless.py:1080` 的 `if native_loop:` 决定走 `_run_native_async`（native
  session / trace / checkpoint）还是 v4 session 分支（`:1092` 起）；
- CLI 开关在 `src/deeppyro/tui/main.py:48-58`（互斥组）定义，`:141-159` 映射为
  `native_loop: bool | None`（默认 `None`），`:180-191` 透传给 `run_once`。

注意：`run_once` 的默认值从 `False` 改成了 `None`（sentinel）。这不是风格问题——`None`
表示“交给环境变量/新默认”，`False` 表示“无条件钉住 v4”。两者对库调用者语义不同。

## 3. CLI 示例与弃用别名

```powershell
# 默认：native loop（schema 1 会话，~/.dpro/sessions/<session-id>.jsonl）
uv run dpyro --headless "总结这个仓库的目录结构"

# 回退 v4 路径（等价 DEEPPYRO_NATIVE_LOOP=0）
uv run dpyro --headless --legacy-v4 "用旧 v4 路径跑一次"

# 环境变量仍是受支持的非 CLI 开关；显式 flag 优先于环境变量
$env:DEEPPYRO_NATIVE_LOOP = "0"
uv run dpyro --headless "走 v4"
Remove-Item Env:DEEPPYRO_NATIVE_LOOP
```

弃用别名（仍可用，但两条链路都会告警）：

```powershell
uv run dpyro --headless --native-loop "显式选择 native（其实已是默认）"
```

`--native-loop` 同时产生：stderr 上的人类可读警告，以及 Python `DeprecationWarning`
（`src/deeppyro/tui/main.py:145-157`；普通运行默认过滤后者，`-W error`/pytest 中可升级为
错误）。脚本与 CI 应改用默认路径，需要旧行为时用 `--legacy-v4`，不要继续依赖该别名。

## 4. run_once 库契约

| `native_loop` 取值 | 生效路径 | 备注 |
| --- | --- | --- |
| 省略 / `None` | 由 `DEEPPYRO_NATIVE_LOOP` 决定，默认 native | 与 CLI 默认一致 |
| `True` | native | 显式钉住，优先于 env |
| `False` | v4 | 显式钉住，优先于 env；测试/回放可用它固定路径 |

约定：

- 不 dual-write：一次调用只写一种会话格式，切换后端不会自动迁移旧日志；
- 需要 0.2 旧行为的库调用者必须显式传 `native_loop=False`，不要依赖环境变量；
- `native_loop_enabled()` 在 `headless.__all__`（`src/deeppyro/headless.py:1165-1181`）中导出；
  需要预判“当前生效后端”的脚本应调用它，而不是复制一份 env 解析逻辑；
- 会话/trace 路径见第 5 节；跨格式读取的唯一途径是 `dpyro migrate`。

## 5. 升级影响与会话迁移

对 headless 用户是行为变更：升级后未加开关的调用会写 native 会话，而不是 v4。依赖
`session.v4.jsonl` 的脚本、日志采集或回放工具必须显式加 `--legacy-v4`（或
`DEEPPYRO_NATIVE_LOOP=0` / `run_once(native_loop=False)`），或改用 native reader。

- native 会话：`~/.dpro/sessions/<session-id>.jsonl`（`DEEPPYRO_SESSION_DIR` 可覆盖），
  schema 1，只追加；trace 在 `~/.dpro/traces/<run_id>.jsonl`（hash/计数/时延，不含正文）。
- v4 会话：`session.v4.jsonl`（`.zstd` 兼容读），由 TUI 与 `--legacy-v4` 写入。
- 一次性迁移：`dpyro migrate --to v4|native`，安全保证（原子写、写前读回、备份）见
  `docs/guides/session-migration.md`。
- 已知摩擦：默认 headless 现在写 native 会话，而 TUI 仍是 v4 reader，`/sessions` 列表
  看不到这些新会话；要让 TUI 打开，先 `dpyro migrate --to v4`（见第 6 节）。

## 6. TUI 迁移状态（KI-001 partial）

**本次变更不改 TUI 行为。** TUI 仍由 v4 session/agent 驱动：

- 启动路径在 `src/deeppyro/tui/main.py:161-172`：无 prompt 时进入 `DeepPyroApp`，
  不经过 `run_once`，也不读取 native loop 开关；带 `--legacy-v4`/`--native-loop`
  启动 TUI 只会打印一行 note（`:163-167`），不改变后端。
- TUI 的会话存储来自 v4 store：`src/deeppyro/tui/runtime.py:84,109` 使用
  `builtin.sessions` 的 `SessionStore`（`src/deeppyro/builtin/sessions.py:25`），
  底层是 `<projectKey>/<sessionId>/session.v4.jsonl` 布局（`src/deeppyro/session/store.py:5`）。
  native 的 `<session-id>.jsonl` 不在这个 store 里，因此 `/sessions`、`/resume` 看不到。
- 未迁移原因：TUI 面覆盖 approval / image / compaction / MCP / PTC / subagent / team /
  job / schedule / hook 等交互，而 native 差分基线仍有覆盖缺口（README“已知限制”的
  native 覆盖缺口条目、`docs/release/0.2.0-known-issues.md`）；迁移需要 native 侧的
  approval/中断/恢复接线与双格式 session reader，不是一次开关重命名。
- 结论：KI-001 在 0.2.0 线是 **partial**——headless 默认切换完成，TUI 迁移未完成；
  v4 provider / `core.wire` 在 TUI 迁移前不能删除，完整关闭留给后续版本。

## 7. KI-010：v4 wire 弃用边界与防回潮

`src/deeppyro/core/wire.py` 是 v4 协议层（on-disk camelCase 字段名是兼容契约，不能重命名）。
native loop 使用 `deeppyro.core.messages` 与 `deeppyro.session.schema`，不得 import 该模块；
唯一的新增允许缝是 `deeppyro.agent.native_provider`（把 v4 模型目录适配给 native loop）。
模块级弃用说明在 `src/deeppyro/core/wire.py:6-12`，明确写了：

- 弃用状态（KI-010）与 native 的替代模块；
- 移除条件：TUI 迁出 v4 + 0.3 provider port 完成、v4 provider/reader 删除后，随 v4 栈
  一起删除 `core.wire`；
- 防回潮测试的位置（下沉到 CI，见下）。

防回潮 guard 在 `tests/integration/test_wire_boundary.py`，用 AST 扫描而不是子进程 import：
`import deeppyro.agent.loop` 会经 `core.protocols` 传递加载 `core.wire`，子进程 import
断言会误报，AST 只看直接 import。扫描器同时解析 `import deeppyro.core.wire`、
`from deeppyro.core import wire` 与相对 import（`:58-92`），五种 import 写法逃不掉。
五个测试：

- `test_native_footprint_does_not_import_core_wire`（`:95-100`）：12 个 native footprint
  模块（`agent/{budget,checkpoint,context_plan,errors,events,loop,run_state}.py`、
  `session/{fold,schema}.py`、`observability/trace.py`、`core/{ids,messages}.py`，清单在
  `:26-39`）不得 import `deeppyro.core.wire`；
- `test_observability_package_is_wire_free`（`:102-108`）：整个 `observability/` 包 wire-free；
- `test_agent_wire_importers_are_the_known_v4_surface`（`:111-121`）：`agent/` 下 wire importer
  必须等于已知 v4 表面 allowlist（`:43-55` 的 9 个文件，含唯一缝 `native_provider.py`），
  新增即失败；allowlist 只减不增；
- `test_import_scanner_catches_wire_import_styles`（`:124-138`）：扫描器自测，保证改 import
  写法（含相对 import）不会静默绕过 guard；
- `test_core_wire_documents_its_deprecation`（`:141-147`）：wire docstring 必须仍带
  `Deprecated` / `KI-010` / `native`。

删除 v4 时同步动作：删 `core/wire.py`；移除本 guard 文件与 allowlist 中所有 native-adjacent
条目；更新 `docs/release/` 的 KI-010 状态与 ADR 里的 importer 计数。

## 8. 验证

```powershell
.venv\Scripts\python.exe -m ruff check src/deeppyro/headless.py src/deeppyro/tui/main.py src/deeppyro/core/wire.py tests/integration/test_wire_boundary.py tests/native/test_headless_native.py tests/integration/test_observability_services.py tests/tui/test_headless.py
.venv\Scripts\python.exe -m ruff format --check src/deeppyro/headless.py src/deeppyro/tui/main.py src/deeppyro/core/wire.py tests/integration/test_wire_boundary.py tests/native/test_headless_native.py tests/integration/test_observability_services.py tests/tui/test_headless.py
.venv\Scripts\python.exe -m pytest -q --no-header -p no:cacheprovider tests/integration/test_wire_boundary.py tests/native/test_headless_native.py tests/integration/test_observability_services.py tests/tui/test_headless.py
```

覆盖点（全部离线，脚本化 provider，不访问网络）：

- 默认路径落 native：`tests/native/test_headless_native.py:274` 的
  `test_run_once_defaults_to_native_loop` 与 `:362` 的 `test_cli_default_runs_the_native_loop`；
- 默认值语义：`tests/native/test_headless_native.py:112` 的
  `test_native_loop_enabled_defaults_to_native`；
- 三种开关组合：同文件 `:128-150`（parser 默认/互斥、`None/True/False` 透传）与
  `tests/tui/test_headless.py:937-1005`（含 `--legacy-v4` 在 env=1 时仍走 v4、
  `--native-loop` 触发 `DeprecationWarning`）；
- 显式 `False` 强制 v4：`tests/native/test_headless_native.py:346`
  `test_run_once_native_loop_false_forces_legacy_v4`；
- native 分支观测接线：`tests/native/test_headless_native.py:293`
  `test_run_once_native_reports_usage_and_trace_export`（usage/trace_export 都被 loop 消费）
  与 `tests/integration/test_observability_services.py`（真实 kernel boot 下验证 `_plugin_services()` 键名）；
- v4 回归：`tests/tui/test_headless.py` 用 autouse fixture（`:142-144`）把该模块固定为
  v4 路径；native 默认由 `tests/native/` 覆盖；
- wire 边界：第 7 节的五个 guard 测试（含扫描器自测）。

## 9. 与发布文档的衔接

- `docs/release/0.2.0-known-issues.md` 的 KI-001 / KI-010 行不在本次改动范围，由发布
  文档维护者按本文与代码现状更新（KI-001 partial、KI-010 partial）；
- `docs/spec/`、`docs/native/`、`docs/plugins/` 中仍写“native 是 opt-in / 默认 v4”的段落
  需要另行同步（不在本指南范围）；
- README 已按默认化更新：状态块、CLI 表、`sessions/` 目录说明、已知限制与文档导航。
