# 遥测与数据上报（Telemetry）

| 项目 | 值 |
| --- | --- |
| 文档 | `docs/ops/telemetry.md` |
| 状态 | 0.2.0 **未实现遥测**；本文只冻结接口边界与默认值 |
| 适用版本 | 0.2.0（基线 `35b433f`，M5 文档/发布线） |
| 结论 | 默认无遥测、无网络上报、无第三方分析 SDK |

## 1. 结论摘要

- **默认不采集、不上报、不联网。** 0.2.0 的运行时不会因为启动、对话、工具调用或崩溃而向任何远端发送数据；仓库中也没有后台上报线程、定时任务或埋点代码。
- **唯一的观测数据是本地文件。** 会话日志（`session log`）与 trace store 都只写在本机用户目录下，由用户完全掌控，可以直接删除。
- **不与任何上游遥测端点兼容。** 0.2.0 不读取 DSH/其他 harness 的遥测配置，也不复用其 telemetry 库或端点；DSH 事件目录里出现的 `session-telemetry/record` 只是逆向得到的名称映射，不产生发送行为（见 §2）。
- **未来若引入遥测，必须显式 opt-in。** 约定见 §4；在实现并写入 release note 之前，任何默认开启的上报都视为缺陷。

## 2. 代码现状（审计结论）

审计范围：`pyproject.toml`、`src/deeppyro/**`。方法：依赖清单人工核对 + `grep -ri` 搜索 + Python tokenizer 分类。

| 检查项 | 结果 |
| --- | --- |
| 运行时依赖 | `pyproject.toml` 依赖只有 `httpx`、`pydantic`、`rich`、`zstandard`、`typing-extensions`、`textual`、`python-dateutil`、`tzdata`、`pillow`；dev 组只有 pytest/ruff/pyright 等，**没有任何 telemetry/analytics/OTEL/sentry SDK**。 |
| 遥测模块 | `src/` 下没有 `telemetry`、`analytics`、`otel`、`sentry`、`metrics` 之类的模块或后台线程。 |
| DSH 名称命中 | 唯一命中是 `src/deeppyro/core/event_catalog.py:87` 的 `"session-telemetry/record": (("waterfall",), "@deepseek-ai/dsh-session-telemetry/lib/index.js:162")` —— 这是 M0 从 DSH 产物里提取的事件名/调用点映射，**只是常量表，没有发送逻辑**（该表本身随 D2 去 DSH 化在 M2/M3 退役）。 |
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
- 用途：本地回放、调试、成本/时延统计。用户可以直接删除 `traces/`，不会影响会话与派生。
- OpenTelemetry 只是**字段命名对齐计划**（`docs/native/schema.md` §8.6：`provider` → `gen_ai.provider.name` 等）。0.2.0 **不实现 OTLP 导出**，也没有 collector 配置。

## 4. 未来 opt-in 接口约定（0.2.0 未实现）

以下为关闭默认值下的最小约定，供 M5 之后的实现者遵守；具体配置键、payload schema 与保留策略尚未冻结，全部标 `TODO(telemetry)`，不要按本文臆测实现。

1. **默认关闭。** 遥测必须由用户显式配置开启，并显式给出 endpoint；不存在“构建时默认开启”的发行版。
2. **最小数据面。** 只允许发送聚合后的 trace 字段（事件类型、计数、时延分桶、错误码、版本、平台粗粒度信息）。禁止发送 prompt、消息正文、工具参数/输出、文件路径、用户名、API key、会话 id 明文。
3. **本地优先。** 先写本地 trace，再做可选导出；导出失败不得影响主流程，也不得阻塞 session append。
4. **可检查。** 开启前提供 dry-run/导出预览，用户能逐字段检查将发送的内容。
5. **不继承上游。** 不读取 DSH 或其他 harness 的遥测配置，不复用第三方遥测 SDK 的默认端点；如需复用协议，必须写明数据字段映射与 opt-in 流程。
6. **可关闭、可撤回。** 关闭开关立即停止出站；卸载/关闭后不残留后台任务。

目标形状（**示意，未实现**）：

```toml
# TODO(telemetry): 0.2.0 不实现；键名与默认值以未来 ADR 为准
[telemetry]
enabled = false
endpoint = ""
```

## 5. 如何验证“没有遥测”

- 用 §2 的搜索命令检查依赖与源码；CI 不安装任何遥测包。
- 在断网环境运行 `uv run dpyro --headless "hi" --provider <local>`，除模型 provider 外不应出现其他出站连接。
- 检查 `~/.dpro/` 下新增文件：除 `sessions/`、`traces/`、`config.toml` 等本地文件外，不应出现上报队列/缓冲文件。
- 发现任何未声明的出站请求，按安全缺陷处理并在 release note 中披露。

## 6. 变更纪律

- 新增遥测（含崩溃上报、使用统计、A/B 实验）必须先有 ADR，再更新本文与 `docs/release/0.2.0.md`，并补默认关闭测试与隐私测试。
- 不得在 M2/M3/M4 的实现中“顺手”加入上报；M5 发布门禁要求本文所述的关闭默认值在代码中可验证。

## 7. 相关文档

- `docs/native/schema.md` §8（Trace/run 关联、OTel 字段对齐、采样与保留）
- `docs/release/0.2.0.md`（发布说明与已知限制）
- `docs/adr/0001-three-track-native-agent.md`（ADR D2/D3：去 DSH 化范围）

