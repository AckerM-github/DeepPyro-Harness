# 遥测与数据上报（Telemetry）

| 项目 | 值 |
| --- | --- |
| 文档 | `docs/ops/telemetry.md` |
| 状态 | 本地 trace 由 native loop 写；OTLP/HTTP(JSON) 导出器已实现但**默认关闭**；v4 会话/loop 已删除（0.3.0） |
| 适用版本 | 0.3.0（native-only；2026-11-19 文档核对） |
| 结论 | 默认无遥测、无网络上报、无第三方分析 SDK |

## 1. 结论摘要

- **默认不采集、不上报、不联网。** 运行时不会因为启动、对话、工具调用或崩溃而向任何远端发送数据；唯一的出站通道是可选的 OTLP 导出器，必须显式开启并给出 endpoint（§4）。
- **默认的观测数据只写本地文件。** 会话日志（`session log`）、trace store 与 usage 账本都只写在本机用户目录下，由用户完全掌控，可以直接删除；唯一可选的出站通道是显式开启的 OTLP 导出（§4）。
- **不与任何上游遥测端点兼容。** 本项目不读取其他 harness 的遥测配置，也不复用其 telemetry 库或端点；0.3.0 删除外部 DSH 事件目录后，源码中不再有上游事件名常量（见 §2）。
- **未来若引入遥测，必须显式 opt-in。** 约定见 §4；在实现并写入 release note 之前，任何默认开启的上报都视为缺陷。

## 2. 代码现状（审计结论）

审计范围：`pyproject.toml`、`src/deeppyro/**`。方法：依赖清单人工核对 + `grep -ri` 搜索 + Python tokenizer 分类。

| 检查项 | 结果 |
| --- | --- |
| 运行时依赖 | `pyproject.toml` 依赖有 `httpx`、`pydantic`、`rich`、`zstandard`、`typing-extensions`、`textual`、`python-dateutil`、`tzdata`、`pillow`；dev 组只有 pytest/ruff/pyright 等，**没有任何 telemetry/analytics/OTEL/sentry SDK**。注：0.3.0 已无 src 代码使用 `zstandard`（v4 zstd 会话读取已删除），依赖行仍留在 `pyproject.toml`，以构建配置为准。 |
| 遥测模块 | `src/deeppyro/observability/{telemetry,otlp,protocols}.py` 存在，但**默认关闭**：不带 endpoint 时不建后台任务、不发请求；另有 `builtin/usage.py` 写本地 JSONL 账本。没有 analytics/Sentry/metrics SDK。 |
| 上游名称命中 | 0.3.0 已删除 `src/deeppyro/core/event_catalog.py`（旧上游事件名常量表）；当前 `core/events.py` 是 native-only 内核事件目录，不含上游端点/发送逻辑。品牌残留审计由 lead 负责（历史 KI-032）。 |
| 假阳性 | TUI 里的 `ProcessEntry` 等类名会被 `-i telemetry` 式搜索误命中，与上报无关。 |
| 网络出站 | 运行时代码只通过 `llm/` 的 provider transport 访问用户配置的模型 API；没有其他出站目的地。 |

复现命令：

```powershell
git grep -n -i -E 'telemetry|analytics|sentry|otlp|opentelemetry' -- src pyproject.toml
git grep -n -i 'requests\.|urlopen|http\.client|socket\.' -- src
```

## 3. trace store 是本地观测数据，不是遥测

`src/deeppyro/observability/trace.py` 的 trace store 容易被误认为遥测，说明如下：

- 存储位置：默认 `~/.dpro/traces/<run_id>.jsonl`，可用 `DEEPPYRO_TRACES_DIR` 覆盖；只写本地文件，没有上传通道。
- 记录内容：`run` / `model_call` / `tool_call` 三类记录。只保存 `prompt_hash`、`args_hash`（`sha256:<64hex>`）、token 计数、字节大小、时延、`finish_reason`、id 字段；**不保存 prompt 正文、消息正文、工具输出正文**（`docs/native/schema.md` §8.1、§8.5）。
- 覆盖范围：由 native loop 写（`AgentLoop` 每个 run/model call/tool call 写一条）。`src/deeppyro/observability/telemetry.py` 仍保留旧 v4 的 hook/scanner 代码，但 0.3.0 删除 v4 agent 后那些 `agent/*`、`llm/stream` 钩子没有 dispatcher，实际生效的是 native loop 经 `trace_export` 服务转交的记录（字段含 `retry_count`、`latency_ms`、`finish_reason`）。
- 用途：本地回放、调试、成本/时延统计。用户可以直接删除 `traces/`，不会影响会话与派生。
- OpenTelemetry 字段命名对齐见 `docs/native/schema.md` §8.6（`provider` → `gen_ai.provider.name` 等）；**可选的 OTLP/HTTP(JSON) 导出**见 §4，默认关闭。
- 用量账本 `src/deeppyro/builtin/usage.py` 同样是本地文件：默认 `~/.dpro/usage/usage.jsonl`（`DEEPPYRO_USAGE_DIR` 覆盖），逐轮追加 provider/model、input/output/cached tokens、latency、retries、stop reason、session/turn，并支持按 session/day/model 聚合。它不上传，也不进入 trace store。

## 4. 可选的 OTLP/HTTP(JSON) 导出（默认关闭）

`src/deeppyro/observability/telemetry.py`（插件 `telemetry`）提供 `trace_export` 服务：native loop 把已写本地 trace 的 `run`/`model_call`/`tool_call` 记录交给它，由 `observability/otlp.py` 可选导出。插件里旧 v4 的 hook 监听与 `SessionRecordScanner` 代码仍在，但 v4 loop 已删除、这些钩子不再触发，因此当前唯一活跃路径是 native `trace_export`。

导出器 `src/deeppyro/observability/otlp.py` 的约束：

1. **默认关闭。** 只有 `[observability.otlp] enabled = true` 且 `endpoint` 非空才发送；`enabled` 必须是布尔真值，字符串不生效。
2. **本地优先。** 先写本地 trace store，再入队导出；`submit()` 只做非阻塞入队，队列有界（默认 256），满时丢弃并累加 `dropped` 计数；HTTP 失败只计数/记日志（`failed`），不重试、不抛出、不阻塞 agent。
3. **最小数据面。** 只导出 trace 记录的聚合字段（见下表）；**不导出** prompt/消息/工具输出正文、文件路径、用户名、API key 与会话 id 明文（`session_id` 只留在本地 trace store）。
4. **可关闭。** 关闭后不创建后台任务、不产生出站连接；插件 `stop()` 会 flush 队列并关闭 httpx client。

配置：

```toml
[observability]
enabled = true            # 关闭后插件不装旧 v4 监听/本地 TraceStore（默认 true）；native loop 的本地 trace 与 trace_export 不受影响
dir = "~/.dpro/traces"    # 可选，覆盖 TraceStore 根目录

[observability.otlp]
enabled = false           # 必须显式置 true
endpoint = "http://127.0.0.1:4318"   # 自动补 /v1/traces
serviceName = "deeppyro"  # 可选
queueSize = 256           # 可选，有界队列容量
timeoutSeconds = 5        # 可选，单次 POST 超时
# headers = { "x-api-key" = "..." }  # 可选，collector 认证头
```

导出 span 字段（OTLP resource/scope 固定为 `service.name`、`deeppyro.observability`；时间戳由记录的 `ts` 与 `latency_ms` 换算）：

| span | 名称 | 属性 |
| --- | --- | --- |
| run | `run`（kind=INTERNAL） | `deeppyro.run_id`、`gen_ai.provider.name`、`gen_ai.request.model`、`deeppyro.started_at` |
| model_call | `<provider>.<model>`（kind=CLIENT） | `gen_ai.provider.name`、`gen_ai.request.model`、`gen_ai.usage.input_tokens`、`gen_ai.usage.output_tokens`、`gen_ai.usage.cached_input_tokens`、`gen_ai.usage.reasoning_tokens`、`deeppyro.run_id`、`deeppyro.turn_id`、`deeppyro.step_id`、`deeppyro.finish_reason`、`deeppyro.retry_count`、`deeppyro.latency_ms`、`deeppyro.cost_usd`、`deeppyro.error_code` |
| tool_call | `tool.<name>`（kind=CLIENT） | `deeppyro.call_id`、`gen_ai.tool.name`、`deeppyro.args_hash`、`deeppyro.result_size`、`deeppyro.is_error`、`deeppyro.retry_count`、`deeppyro.latency_ms`、`deeppyro.run_id`、`deeppyro.turn_id`、`deeppyro.step_id`、`deeppyro.capability`、`deeppyro.risk`、`deeppyro.error_code`、`deeppyro.idempotency_key` |

`status.code`：`finish_reason ∈ {error, aborted}` 或工具 `is_error` 时为 2（ERROR），否则为 1（OK）。空字段（如 `cached_input_tokens`）不写属性。

## 5. 如何验证“没有遥测”

- 用 §2 的搜索命令检查依赖与源码；CI 不安装任何遥测包。
- 在断网环境运行 `uv run dpyro --headless "hi" --provider <local>`，除模型 provider 外不应出现其他出站连接。
- 检查 `~/.dpro/` 下新增文件：除 `sessions/`、`traces/`、`usage/`、`config.toml` 等本地文件外，不应出现上报队列/缓冲文件。
- 默认配置下不得出现任何 OTLP 请求：`tests/observability/test_otlp.py` 断言关闭时零请求、开启后仅 POST 到配置的 endpoint、队列满只增加 `dropped`、传输失败只增加 `failed`、导出 span 不含 `session_id` 明文；`tests/observability/test_telemetry.py` 覆盖插件的旧 v4 scanner 代码路径（v4 loop 已删除，生产路径以 native `trace_export` 为准）。
- 发现任何未声明的出站请求，按安全缺陷处理并在 release note 中披露。

## 6. 变更纪律

- 新增遥测（含崩溃上报、使用统计、A/B 实验）必须先有 ADR，再更新本文与 release note，并补默认关闭测试与隐私测试。
- `[observability.otlp]` 的键名、span 名称与属性表是公开契约：增删属性必须同时改本节表格与 `tests/observability/test_otlp.py`；任何默认开启或新增出站目的地都视为缺陷。
- 不得在 M2/M3/M4 的实现中“顺手”加入上报；发布门禁要求本文所述的关闭默认值在代码中可验证。

## 7. 相关文档

- `docs/native/schema.md` §8（Trace/run 关联、OTel 字段对齐、采样与保留）
- `docs/release/0.3.0.md`（0.3.0 native-only 发布说明；旧 v4 trace 路径已删除）
- `docs/release/0.2.0.md`（历史发布说明与已知限制）
- `docs/adr/0001-three-track-native-agent.md`（ADR D2/D3：去 DSH 化范围）

