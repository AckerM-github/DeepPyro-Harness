# TUI 贡献面（P2）

本文是外部插件参与 TUI 的契约：有哪些槽位、`ui` 服务如何跨线程推动作、插件声明如何校验、以及宿主与插件各自的红线。实现分布：

- `src/deeppyro/tui/slots.py`：槽位注册表（`SlotRegistry`）与 `SlotsPlugin`；
- `src/deeppyro/tui/ui.py`：Textual-free 的 `UiService`（动作队列）；
- `src/deeppyro/tui/screens/slot_screen.py`：`screen` 槽位的通用宿主；
- `src/deeppyro/tui/health.py`：`/plugins` 屏与 CLI 共用的健康快照；
- `src/deeppyro/api/context.py`：`PluginContext.ui` 便捷入口。

## 1. 槽位清单

`plugin.toml` 的 `[ui].slots` 只允许下列值；`ui.toml` 的 section 与槽位一一对应。

| 槽位 | `ui.toml` section | 宿主渲染点 | 数据来源 | 关键限制 |
| --- | --- | --- | --- | --- |
| `status` | `[[status]]` | `PluginStatusBar`（底部单行） | `build_slot_context(app)` 快照 | 同步、返回 `str`/`Text`；不碰控件 |
| `conversation` | `[[conversation]]` | 会话流（事件行） | 事件 dict | `predicate` 只读事件；返回 widget/`None` |
| `panel` | `[[panels]]` | `PluginPanelScreen`（模态） | 快照 | 工厂同步、只读快照 |
| `settings` | `[[settings]]` | `PluginPanelScreen`（同 panel 宿主） | 快照 | `/settings [id]`（M1 接入） |
| `screen` | `[[screens]]` | `SlotScreen`（整窗模态） | 快照 | 渲染器必须同步；禁止 IO/await |

`SlotRegistry` 的查询按 `(kind, order, id)` 排序。`ui.toml` 用 `id` 声明稳定 id；`screen`/`panel`/`settings` 省略 `id` 时按标题 slug 生成，其余槽位按 callable 的模块名生成。

## 2. `screen` 槽位

### 2.1 注册

```python
registry = self.ctx.require(SlotRegistry)
dispose = registry.add_screen(
    "indexer",                      # 稳定 id，用于 /panel indexer 与 ui.open_screen("indexer")
    title="索引器",
    renderer=render_screen,         # (context) -> Widget | Iterable[Widget] | str | Text | None
    order=100,
    plugin_id=ctx.plugin_id,
)
```

签名：

```python
SlotRegistry.add_screen(
    id: str,
    *,
    title: str = "",
    renderer: Callable[[Mapping[str, Any]], Any],
    kind: str = SCREEN,
    order: int = DEFAULT_ORDER,
    plugin_id: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> Callable[[], None]
```

- `kind` 只接受 `"screen"`，否则抛 `SlotError`；空 id 也抛 `SlotError`。
- 重复 `(kind, id)` 抛 `SlotError`，错误信息包含两个 owner，便于定位冲突。
- 返回的 dispose 幂等；插件 fiber 卸载时由 `ctx._own` 自动调用。
- 查询：`registry.screens()`、`registry.get("screen", id)`、`registry.list("screen")`。

### 2.2 打开

- 用户：`/panel <id>` 先尝试 panel，再尝试 screen；未命中时列出两类 id。`/settings [id]` 打开设置页（唯一时免参数）。插件也可以注册自己的命令，在 handler 里 `ctx.ui.open_screen("indexer")`。
- 插件代码：`ctx.ui.open_screen("indexer")`（任意线程，见 §4）。
- 宿主 API：`deeppyro.tui.screens.slot_screen.open_slot_screen(app, registry, *, slot_id=None, context=None) -> SlotScreen | None`；slot_id 省略时打开排序最靠前的 screen。

### 2.3 渲染器契约

渲染器只接收**只读快照** `build_slot_context(app)`：`app`、`runtime`、`cwd`、`session_id`、`provider`、`model`、`phase`、token 用量、`context_window`、`busy`、`theme`、`locale`、`translate`、`ui_status`。

- 必须同步返回；返回 awaitable 会被宿主关闭并抛 `SlotError("screen renderer must be synchronous (no async/await)")`。
- 只读快照；禁止网络/磁盘/子进程等 IO，禁止 await。
- 禁止 `query_one` / `mount` 核心控件树；需要交互时只在自己的返回子树内使用 Textual。
- 禁止修改 `theme.tcss`、追加全局 CSS 或替换宿主主题。
- 允许返回 widget、widget 可迭代对象、`str`/`Text`、`None`；异常由 `PluginPanelHost` 隔离，屏幕内联显示 `panel failed: ...`，不会拖垮 app。

## 3. `ui` 服务 API

服务名 `ui`，实现 `deeppyro.tui.ui.UiService`。它由 core bundle 的 `SlotsPlugin` 在 `setup()` 中一并 `provide`（不单独占 bundle 行），因此 headless 与 TUI 启动时都可用。

```python
ui = ctx.ui                      # 服务缺失时是 no-op 代理，不会抛异常
ui.available                     # 当前是否绑定了 TUI 后端
ui.post(action, /, **payload)    # 返回 bool：是否成功入队
ui.notify("done", level="info")  # level: info/success/warning/error
ui.open_screen("indexer")        # 省略 id 打开第一个 screen
ui.open_panel("stats")           # 省略 id 打开第一个 panel
ui.set_status("indexer", "12 篇") # 空字符串清除该段
ui.add_row("索引完成", level="info")  # level=error 会走 add_error
ui.refresh()                     # 请求宿主重绘插件 chrome
```

动作类型与 payload：

| kind | payload | 宿主行为 |
| --- | --- | --- |
| `notify` | `text`, `level` | `App.notify`，level 映射 information/warning/error |
| `open_screen` | `slot_id` | 压入 `SlotScreen`；未命中给 warning |
| `open_panel` | `slot_id` | 压入 `PluginPanelScreen`；未命中给 warning |
| `set_status` | `id`, `text` | 更新状态栏段；空 text 删除 |
| `add_row` | `text`, `level` | `conversation.add_info` / `add_error` |
| `refresh` | — | `refresh_status()` |

`post` 接受三种写法：`UiAction`、`"notify"` + 关键字、`{"kind": "notify", "text": "..."}`。未知 kind 不会让 drain 崩溃，宿主会发 warning。

诊断：`ui.pending`、`ui.stats()`（`bound/queued/delivered/dropped/errors`）、`ui.error_messages`。

## 4. 线程模型

- 插件线程 / worker / 回调：任意线程调用 `ui.notify` / `ui.set_status` / `ui.post`。
- `UiService` 内部只用 `threading.Lock` + `collections.deque`，不 import Textual；生产者拿锁入队后立即返回。
- TUI 线程：App 在 `on_mount` 末尾 `ui.bind(self)` 并 `set_interval(0.1, _drain_ui)`；drain 按 FIFO 顺序调用 `app.handle_ui_action`。

- **任何非 UI 线程都不得直接碰 Textual 控件**；唯一通道就是 `ui` 服务。
- 投递是 fire-and-forget：`post` 返回已入队，不保证已渲染；动作没有重试、确认或优先级。
- 无 TUI（headless）时 `post` 返回 `False` 并累加 `dropped`，不抛异常；`ctx.ui` 在服务根本未加载时返回 no-op 代理。
- `on_unmount` 先停 drain 计时器、`ui.unbind()`、`ui.clear()`，再关闭 runtime；退出期间的动作会被丢弃。
- 单个动作处理抛异常只记录到 `error_messages`，后续动作继续 drain。

## 5. 声明与校验

`plugin.toml`：

```toml
[ui]
slots = ["status", "screen"]     # 白名单：status/conversation/panel/settings/screen
package = "ui.toml"              # 可选
```

- 声明期：`parse_metadata(..., known_slots=KNOWN_SLOT_KINDS)` 校验；未知值报 `metadata-invalid: [ui].slots declares unknown slot kind 'x' (known: ...)`。
- 注册期：`SlotRegistry._add` / `add_screen` 对未知 kind 抛 `SlotError`，错误信息列出全部已知槽位；重复 id 也会报清楚的两个 owner。
- 运行期：`SlotsPlugin` 加载 `ui.toml` 时逐条隔离错误；`/plugins` 屏的健康扫描显式传入 `known_slots`，未知槽位以 `metadata-invalid` 行显示。
- `ui.toml` 的 `[[screens]]`：

```toml
[[screens]]
id = "indexer"
title = "索引器"
order = 60
renderer = "acme_ui:render_indexer"
```

## 6. `/plugins` 健康状态

- `deeppyro.tui.health.scan_health()` 调用 `scan_plugin_packages(root, known_slots=KNOWN_SLOT_KINDS)`，再对每个包调用 `deeppyro.plugins.cli.diagnose(package)`。
- `diagnose()` 是 CLI `dpyro plugin doctor` 的同一份实现（`package.errors + dependency_errors + _abi_warnings + bundle_errors`），TUI 不复制任何诊断逻辑。
- 屏幕顶部显示 `健康检查: N 正常 / M 失败`；表格状态列显示 `ok` 或首个失败类别（metadata-invalid / import-failed / dep-missing / dep-conflict / abi-mismatch / version-incompat）；详情页逐条显示 `kind: message` 与 `建议: remediation`。
- 扫描失败（例如插件根目录不可读）显示为快照级扫描错误，不阻塞屏幕打开。

## 7. 红线

1. 非 UI 线程禁止直接操作任何 Textual 控件；只用 `ctx.ui`。
2. 禁止 `query_one` / `mount` 核心控件树；只能在插件自己的返回子树内组合控件。
3. 禁止追加全局 CSS、修改或替换 `theme.tcss` 与宿主主题。
4. 禁止把 widget 属性命名为 `_context`：Textual `MessagePump` 定义了 `_context()`，遮蔽会让 mount 永久挂起；宿主统一用 `_slot_context`。
5. `status` / `panel` / `settings` / `screen` 渲染器必须同步、只读快照；禁止 IO 与 await。
6. 禁止自定义槽位 kind；只使用第 1 节的白名单。
7. 禁止替换、删除会话行或重放历史；`conversation` 槽位是追加式。
8. 禁止插件自己 `push_screen_wait` / `run_worker` 绕过宿主调度；需要弹窗时用 `ui.open_screen` / `ui.open_panel`。

## 8. 最小示例

```python
# acme_ui.py
from textual.widgets import Static


def status_text(context):
    return f"acme: {context['phase']}"


def render_indexer(context):
    return Static(f"会话 {context['session_id']} / 模型 {context['model']}")
```

```toml
# plugin.toml
[ui]
slots = ["status", "screen"]
```

```toml
# ui.toml
[[status]]
id = "acme-status"
renderer = "acme_ui:status_text"

[[screens]]
id = "indexer"
title = "Acme 索引器"
renderer = "acme_ui:render_indexer"
```

```python
def handler(host):
    # 任意线程：
    ctx.ui.notify("索引开始")
    ctx.ui.open_screen("indexer")
```

## 9. 测试与诊断

```powershell
.\.venv\Scripts\python.exe -m pytest tests/tui/test_contributions.py -q
.\.venv\Scripts\python.exe -m pytest -q
ruff check src tests
```

`/plugins` 屏可交互验证：`/plugins` 顶部健康汇总，`i` 查看失败类别与补救建议。CLI 对照：`dpyro plugin doctor`。

## 10. 已知限制

- `settings` 槽位由 `/settings [id]` 消费：只有一个页面时直接打开，多个页面时先列出 `id (title)`，无页面时给一句提示；渲染器约束与 panel 相同。
- `ui` 动作无优先级、无确认、无重试；大量高频动作共享 0.1 秒批次的 200 条上限。
- CLI `dpyro plugin list/doctor` 仍以默认参数调用 `scan_plugin_packages()`，未传 `known_slots`；未知 `[ui].slots` 目前在 TUI `/plugins` 健康扫描和注册期报错中可见，CLI 表格与 doctor 暂不显示（`src/deeppyro/plugins/*` 不在 P2 改动范围）。
- `screen` 渲染器禁止 IO 是契约约束，宿主不做静态分析，只能靠评审与测试保证。
