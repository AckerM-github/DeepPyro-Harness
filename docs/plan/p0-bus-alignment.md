# P0 总线对齐执行方案（deeppyro-bus-alignment.md §P0）

- 输入：`D:/dshworkspace/deeppyro-bus-alignment.md`（§0 决策、§P0、§7 commit 序列、§8 验收）
- 基线：D:/code/deeppyro HEAD `bb28ddd`，工作区干净，全量 2380 passed
- 状态：P0 已开工；P1/P2 待 P0 验收后另立 plan
- 原则：P0 只修**写入口径**与**观测闸门**，不改 hook 语义（waterfall 极性翻转属于 P1-1）

---

## 1. 决策（沿用对齐文档 §0）

| 编号 | 决策 | 落地 |
| --- | --- | --- |
| D1 | 总线结构选 A：双总线 + kernel 桥接 | P0 不动结构，仅收敛常量与 import 边界 |
| D2 | waterfall 极性选 A：翻转对齐 DSH（不调 next = veto） | P1-1，P0 不做 |
| D3 | 对齐边界 = 承诺「模式语义 + payload 字段」，不承诺 DSH 插件源码可移植 | 全部文档措辞 |

P0 期间不引入新的 hook 名，只补 payload 字段（工具三件套的 `agent`；pre-step 的 `tools` 保留并标注为 deeppyro 扩展）。

## 2. 任务拆分（三个并行实现任务 + Lead 收尾）

### P0-1（session-dev）：session 写入口径保真

1. `request/header`：删除 `turn`/`step`；按 DSH `buildRequest`（`_dsh_src/dsh-agent-loop/lib/index.js:1029-1044`）写
   `{header, reason}`，在 initial/resume/change 且 startsSeries 时追加 `startsSeries: true`；
   reason=`series` 的分支**不带** `startsSeries`。`startsSeries` 由现有 `starts_series`（每 turn 首次请求）驱动。
2. `request/context`：删除 `turn`/`step` 形参与 payload；只写
   `{provider, model, contextWindow?, systemPromptUpdate?}`，四字段逐一比较，无变化不写。
3. `tool/result`：新增可选 `error` 字段（`ToolResult.error`，DSH `pipeline.js:196-235` 的形状 `{name, code}`）；
   取消路径对齐 `pipeline.js:909-946`：
   - 已分发未完成的调用 → 文案 `Error: tool call aborted`、`error={"name":"AbortError","code":"ABORTED"}`；
   - 从未分发的调用 → 文案 `Error: tool call aborted before dispatch`、`code="ABORTED_BEFORE_DISPATCH"`。
   不再写 `Error: interrupted`。
4. 测试：字段级断言 + 取消场景 + 更新受影响的 tests/agent；不改读侧校验（session/events.py 已支持 data.error）。

### P0-2（llm-dev）：第一个 hook 消费者 llm-retry

- 新增 `src/deeppyro/builtin/llm_retry.py`（`provide="retryPolicy"`），从 `[retry]` 配置构造
  `deeppyro.llm.retry.RetryPolicy`，注册 `agent/request-error` 监听器：payload 无 policy 时 `await next()`，
  有 policy 时返回 `{"kind":"retry"}`。退避、上限、`llm/retry`/`llm/retry-started` 记录仍由
  `AgentSession` + `agent/retry.decide_retry` 负责（避免双写）。
- 接线：`bundle.toml`（hooks 之后）、`tests/kernel/test_bundle.py` 顺序断言、
  `tui/runtime.py` 与 `headless.py` 的 `_plugin_services()` 加 `retryPolicy`。
- 验收：429/超时失败一次后重试成功，session 日志出现 `llm/retry` 与 `llm/retry-started`（当前永不产生）。

### P0-3 + P0-4（tools-dev）：常量收敛、import 闸门、工具 hook payload

- `tools/pipeline.py` 的裸事件名改用 `deeppyro.hooks` 常量；grep 其他裸 hook 名一并收敛。
- 新增 `tests/architecture/test_import_boundaries.py`：`deeppyro._vendor.cordis_port` 只允许出现在
  `src/deeppyro/kernel/`、`src/deeppyro/api/`；`builtin/inspect.py` 的 `FiberState` 改为经 `api` 重导出。
- `tools/pre-execute`、`tools/execute`、`tools/post-execute` payload 增加 `agent`（必存，默认 None）与
  `toolSet`（可选）；`run_tool_batch`/`run_tool_call` 增加 keyword-only `agent`/`tool_set`，默认 None，
  不改变既有决策语义。`session._execute_batch` 传 `agent=self` 由 Lead 在合并后接线。

## 3. Lead 收尾

1. 接线：`AgentSession._execute_batch(agent=self, tool_set=surface 名字)`；检查 PTC executor 是否需要显式 None。
2. 文档：`docs/spec/loop.md` §3/§4（header/context/error 口径、默认有 llm-retry 消费者）、
   `docs/spec/hooks.md` §5（payload 字段 + pre-step `tools` 标注为扩展）、
   `docs/spec/INTERFACES.md` 相关行、`docs/spec/session.md` 的 request/header 说明。
3. 验证：`uv run pytest -o addopts= -q` 全量 + `uv run ruff check src tests docs` +
   `ruff format --check`；真实模型 429 冒烟（可用假 provider 断言记录，真实 429 不强制）。
4. 提交按对齐文档 §7 的顺序拆成多个 commit（session / llm-retry / hooks 常量与工具 payload），禁止一个大提交混三类改动。

## 4. P0 验收清单（对齐文档 §8）

- [x] request/header、request/context 与 DSH 字段级一致（无 turn/step，startsSeries 语义正确）
- [x] tool/result 携带 error（AbortError + ABORTED / ABORTED_BEFORE_DISPATCH），取消路径有测试
- [x] 429/超时场景产生 llm/retry 与 llm/retry-started
- [x] tests/architecture 存在且全绿；pipeline 无裸事件名
- [x] 工具三件套 payload 带 agent；pre-step 的 tools 已文档化为 deeppyro 扩展
- [x] 全量测试与 ruff 全绿

## 6. 执行记录（2026-11-14）

- 全量：`uv run pytest -o addopts= -q` → **2432 passed in 108.91s**（P0 前基线 2380）。
- 静态：`uv run ruff check src tests docs` 全绿；`ruff format --check` 430 files already formatted。
- 真实模型冒烟（deepseek/deepseek-flash，headless --json，临时 DEEPPYRO_HOME，已清理）：一次 turn 两步，
  `request/header` 1 条 `{header, reason:"initial", startsSeries:true}`；`request/context` 1 条
  `{provider, model, contextWindow, systemPromptUpdate}`（无 turn/step）；`tool/result` 无 `error` 键且
  `isError=false`；无 `llm/retry`；`turn/end` reason=`{"kind":"completed"}`。第二步 header/context 未变时
  不重复落盘，符合 DSH“变化才写”语义。
- 429 重试路径：`tests/builtin/test_llm_retry.py` 集成用例用假 provider 实跑 429 →
  `assistant/attempt → llm/retry → llm/retry-started → assistant/message`，policyKey
  `["normal",5,["RATE_LIMIT","SERVER"],0,1000,0]`（真实 429 不强制）。
- 分 commit：session 保真 / llm-retry 消费者 / hooks 常量+payload 三类分开提交，文档与 plan 单独一批。

## 5. 不做（P0 边界）

- 不翻转 waterfall 极性、不加 scoped handlers、不加 bail/parallel/once/global（P1）
- 不写 `hook/invoked`/`hook/result` 记录、不做 kernel 桥（P2）
- 不硬抄 DSH 187 个事件名；不改 `_vendor/cordis_port` 源码
