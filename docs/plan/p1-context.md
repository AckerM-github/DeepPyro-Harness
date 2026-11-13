# P1 上下文调研：compress / projection checkpoint / session-query / stream chunks

- 作者：session-dev（task-32）
- 日期：2026-11-14
- 状态：调研结论（只读；本文件是唯一交付物，未改 src/ tests/）
- DSH 源码根：D:/dshworkspace/_dsh_src/<包>/（README + bundled lib/*.js）
- 本仓库根：D:/code/deeppyro

---

## 0. 结论速览

1. **compress 不是 DSH compaction 包的工具**。DSH 官方 compaction 是 human `/compact` 命令 + 自动 pressure 引擎（dsh-compaction / dsh-compaction-basic）；fixture 里出现的 `compress` 工具来自宿主上下文管理服务 billion-context（DSH host），其 block 全文**从不写入 session 日志**，只存在宿主 block store。照搬这个工具意味着我们还要自建 block store + m 引用映射。
2. **projection checkpoint 与 session checkpoint-policy 是两件事**。DSH 的 flush 屏障（请求前/工具前/步边界）我们已有等价物（Session.append 立即 flush+fsync）；P1 真正缺的是 per-session 增量投影注册表 + 持久 checkpoint（rows: key -> {ver, seq, val}）与 restore。
3. **session-query 是投入产出比最高的一个**：目前 TUI 会话列表 `runtime.recent_sessions()` 对每个文件做**全量解码**（trimmed 样本已达 37MB 原始/12MB zstd），加一个只读 header+tail 的查询服务能立刻消除这个瓶颈，且不改任何 P0 契约。
4. **stream chunks 是唯一必须动 core 冻结契约的 feature**。P0 的 `core.protocols.StreamEvent` 只有 6 个简化事件，丢掉了 block index / block-start / block-end / tool-call name delta / cacheWriteTokens / reasoningTokens；持久化格式本身（assistant/message.stream）已与 DSH 一致，缺的是协议保真度与 typed readers。
5. fixtures 无法验证 compress 的 checkpoint 形状：30 个真实会话里**没有任何 compact/compress 记录**（surfaceOp replace 只有 8 次 developer 退休 + 1 次 system 替换）。compress 的落盘格式只能按现有 validator + DSH compaction 事务推断，属于**推断设计**，需要真实样本或 Lead 认可。
6. 建议实施顺序：**session-query（低风险）→ projection → stream chunks（需 Lead 批准 core 改动）→ compress（最大工作量，且依赖 block store 决策）**。

---

## 1. P0 基线（我们已有什么）

### 1.1 session.v4 日志（src/deeppyro/session/）

公开面（docs/spec/session.md:1-247）：

- Store：`SessionStore(root=None)` 的 `create(cwd, agent_preset=None, session_id=None)` / `open(session_id)` / `list()` / `latest()`；`SessionInfo(id, path, cwd, createdAt, title, turns, recordCount, agentPreset)`。
- Session：`header` / `records` / `path` / `session_id` / `last_seq` / `append(type, data, **envelope) -> int` / `derive_messages(strict=None)` / `close()` / `usage()` / `turn_count()` / `last_step()`。
- 纯函数：`fold_surface(records, *, strict=True)`、`event_message(record)`、`derive_messages(records, *, strict=True)`、`read_session_file`、`read_header`、`write_new_session`、`append_events`、`encode_event_line`、`decompress_zstd_frames`、layout helpers（`project_key` / `encode_segment` / `session_file_path` / `flat_session_path` / `discover_session_files`）、`fork_session` / `build_fork_seed` / `open_turn_closers` / `CLOSER_TEXT` / `SessionRead`。
- 不变量：单行紧凑 JSON + \n，append 后 flush+fsync；seq=last+1 严格递增（trimmed 例外允许 gap）；header 行不带 seq/time/data；信封只允许 type/seq/time/data + surfaceOp/sourceEventSeqs/ignorable；surface 类型必须带 surfaceOp，其他已知类型禁止；sourceEventSeqs 严格递增且全部 < seq；assistant/message 不得带 sourceEventSeqs。
- canonical surface fold：append -> nodes.push(seq)；replace -> 在 nodes 中找到 start/end 下标，校验 shadow 覆盖与 node0 system 特例后原位 splice；derive 按 nodes 顺序投影，空 content 的 system/developer/assistant 投影为 None。
- 实测：30 会话 / 8138 事件 / 1868 条 surface 记录 / 1859 个最终节点 / 1851 条消息 / 213 个测试全绿。

**已知性能缺陷（本调研直接证据）**：`SessionStore.list()` 对每个发现文件调用 `read_session_file` 做完整解码与逐行校验；`tui/runtime.py:601-603` 的 `recent_sessions()` 直接 `list(self.store.list())[:limit]`，TUI 每次打开会话选择器都会解码所有会话（最大样本 decodedSize 37MB / recordCount 16138）。

### 1.2 compact（src/deeppyro/compact/）

- `Compactor(provider, model, keep_recent_tokens=8000, context_window=32768, threshold=0.85, max_tokens=None)`：`should_compact(messages)` 与 `async compact(session, messages=None, *, source_command_id=None) -> bool`。
- 事务：compaction/start -> LLM 摘要 -> compaction/summary -> 单条 user/message checkpoint（surfaceOp replace，sourceEventSeqs=[start, summary, *shadowed]）-> compaction/end；失败追加 compaction/end{error: errorChain}。
- `prompts.py`：CHECKPOINT_PREAMBLE、SUMMARY_OPEN_TAG/CLOSE_TAG、固定 8 段 COMPACTION_INSTRUCTION、frame_summary。
- `surface.py`：SURFACE_TYPES、fold_surface、tool_pairing_cuts、balanced_before/after、derive_node_message、surface_messages、select_region（永不压缩 node0、不拆 tool 对）、entry_state（open turn / unmatched start / end-seed）、latest_tool_schemas。
- `tokens.py`：CHARS_PER_TOKEN=3.5、MESSAGE_OVERHEAD=4.0、estimate_text/message/messages_tokens（忽略 image/file）。
- **缺口**：没有 model-facing 工具、没有 block store、没有 search_context/decompress、没有 token-meter 的三类投影（tokenUsage/contextPressure/contextBreakdown）与 provider usage 锚点。

### 1.3 agent 的流式与持久化（src/deeppyro/agent/）

- `session.py:660-711` `_stream_step`：每个 provider attempt 新建 `AssistantStreamAccumulator`；LlmError 时补 Finish(kind="error") 并落 assistant/attempt；成功路径返回消息。
- `session.py:737-796` `_consume_stream`：TextDelta -> `{type:"text-delta", index:0, text}`；ReasoningDelta 同样 index 0；ToolCallStart -> 动态分配 index，push tool-call-delta（name + 空 argumentsDelta）；ToolCallArgsDelta -> 同 index + name；Usage -> push；Finish -> push；未知事件抛 LlmError。
- `session.py:1121-1158` `_append_assistant`：assistant/message 的 data 含 turn/step/message/stream（`self._stream.snapshot()`），interrupted 时加 interrupted，usage 有则加。
- `session.py:1198-1209` `_append_assistant_attempt`：assistant/attempt{turn, step, stream}，空流补 finish error chunk。
- `stream.py`：AssistantStreamAccumulator 已与 DSH 对齐（text-chunks/reasoning-chunks/tool-call-chunks/chunk，time0/dt/texts/args，MAX_SAFE_INT）；刻意偏离：MAX_MERGE_GAP_MS=60000 且拒绝负 gap；expand_stream 可反向展开。**没有** DSH 那套 typed readers。
- `core/protocols.py:50-86`：TextDelta(text)、ReasoningDelta(text)、ToolCallStart(call_id,name)、ToolCallArgsDelta(call_id,delta)、Usage(input_tokens,output_tokens,total_tokens,cached_input_tokens)、Finish(kind,failure)；`StreamEvent` 仅这 6 类。

### 1.4 服务与插件接线（写设计必须用的真实名字）

- 插件基类：`deeppyro.api.Plugin` / `Service`（api/plugin.py:36-85），Service 要求类属性 `provide`，构造函数 `__init__(ctx, config)`；`inject: ClassVar[list[str]]` 声明依赖；`setup(ctx, config)` 注册贡献。
- 已有服务：llm、sessions（builtin/sessions.py:12-36，`provide="sessions"`，`store=SessionStore(root=session_dir(cfg))`）、sandbox、tools（builtin/tools.py:15-118，`ToolsPlugin.add(factory, *, scope=None, plugin_id="", revision=None) -> remover`）、agents、jobs（builtin/jobs.py:32）、activity（builtin/activity.py:279）、hooks（builtin/hooks.py:25）、team（builtin/team.py:31）、subagent（builtin/subagent.py:31）、slots（tui/slots.py:442）、inspect、settings、plugin_manager。
- `tui/runtime.py:112`：`services = {name: kernel_app.service(name) for name in ("llm", "sessions", "sandbox", "tools", "agents")}`。
- `tui/runtime.py:294-310` `_plugin_services()` 只把 llm/hooks/plugin_manager/inspect/settings/jobs/team/subagent 放进 `ToolContext.services`：**sessions 不在其中**，模型工具当前拿不到 Session 对象。
- 工具工厂签名是 `factory(config, workspace)`（builtin/tools.py:98）；因此需要 Session/Store 的新工具要么自己按 config 构造 Store，要么把 sessions 加进 `ToolContext.services`。
- `ActivityService`（builtin/activity.py:121-247）：单例、`refresh(session)` 每次遍历 session.records 全量重折叠（todo/write、job/registered|settled、team/member、subagent/descriptor|catalog、plan/mode），`subscribe(listener) -> unsubscribe`，payload 相同返回同一对象。
- `builtin/inspect.py` 是插件宿主自检，不是 session-query。

### 1.5 与 DSH 的能力差距总表

| 能力 | DSH | DeepPyro P0 | P1 缺口 |
| --- | --- | --- | --- |
| 会话枚举 | sessionQuery.listSessions（live-first、header+摘要） | Store.list 全量解码 | 轻量 list + 摘要 |
| 会话查询 | listSessions/readSession/readSurface/listEvents/filterEvents/readEvent/traceSession/traceEvent/search* | 无 | 精确读取服务；搜索列 P2 |
| 投影 | sessionProjections 注册表 + snapshot/checkpoint/restore + 投影缓存 | ActivityService 单域全量重放 | 通用注册表 + 持久 checkpoint |
| 压缩工具 | 宿主 billion-context compress/search_context/decompress（非 DSH 包） | 无；只有 Compactor 引擎 | compress 工具 + block store（决策项） |
| 流式分片 | 7 类 StreamChunk + block index + typed readers | 6 类简化事件、index 丢失 | 协议保真 + readers |

---

## 2. Feature A：compress 工具

### 2.1 DSH/宿主的真实语义（证据）

**工具身份**：对 D:/dshworkspace/_dsh_src 下所有包的 `lib/*.js` 检索 `compress`，只命中 session-format / session-persistence 的 zstd 解压字样；dsh-compaction 的 README 明确说官方压缩是 human `/compact`，model-facing tool 尚未决定。真正的 `compress` 工具来自本 DSH 会话的宿主上下文管理（billion-context），其实现不在 _dsh_src。

**schema（fixture request/header 的 tools 数组精确摘录）**：

    {
      "type": "object",
      "properties": {
        "content": {
          "type": "string",
          "description": "ONE plain string holding ALL ranges to compress — each block starts with its 'm00150–m00220 optional topic' header line followed by that block's summary markdown (a valid JSON string: internal quotes \" and newlines \\n; a JSON-encoded array as that string is also accepted). Batch multiple ranges into ONE call — do not split into one call per range."
        }
      }
    }

工具 description 还要求：用 refs 标识范围、单字符串承载所有块、只压缩确实消费过的内容、问题上报 github.com/ranxianglei/billion-context。

**调用与落盘形态（fixtures 实测）**：

- 直接调用：`tool/call name="compress"`，arguments 为 `{"content": "..."}`；共 8 次，分布在多个会话（例：fixtures/sessions/04bb4baa...jsonl seq179）。
- run_code/PTC 内部嵌套：外层 tool/call 是 run_code；内层 `tool/ptc-dispatch-start` + `tool/ptc-dispatch`，data 为 `{rootCallId, parentCallId, subCallId:"<root>:ptc:N", name:"compress", arguments:{content:...}}`；共 8 次、5 个会话（例：fixtures/sessions/7fdb12e1-c6c4-4059-8a53-e7d9d566de7c.jsonl seq171/172）。子调用不产生独立 surface 消息。
- 结果：`tool/result` role=tool、source={kind:"tool",callId}、isError=false、无 meta，message.text 是固定格式文本；共 11 条以 `[Compressed` 开头。样例（04bb4baa seq180，surfaceOp append，sourceEventSeqs=[179]）：

    [Compressed m00005–m00072 → 1 block(s), ~51086 tokens saved.]
     · b1 full summary 4385ch [2026-11-11 22:36:59 – 2026-11-11 22:41:25] · head "目标与约束 - 用户问题（m00001，中文）：「刚刚电脑意" … tail "/上下文、避免反复加载）；④ 若复现用 Driver Verifier 抓驱动。  TASK AS OF THIS BLOCK：只读排查已完成并得到明确结论，最终答复尚未发送；修复/提权动作未获授权。"
    Fingerprints show head/tail only — every summary is stored in full. To retrieve a specific detail from a new block, call search_context({ query }) first; decompress only if the excerpt doesn't answer it.

    No compressible ranges remain — the context is already at its minimum; continue the task without compressing.

- block 头格式：`m00150–m00220 可选主题`，分隔符是 U+2013 en dash；真实样本：`m00005–m00072 Windows 崩溃诊断（0x10E 蓝屏，只读排查）`、`m00126–m00245 再次检查C盘 + D盘空间能否分给C（2026-11-10）`。
- **关键事实**：block 全文（b1 的 4385ch 摘要）不写入 session 日志；日志里只有 compress 的 tool/call 与 tool/result，后续消息引用 mNNNNN。mNNNNN 是宿主会话视图的消息 ref（按会话 ingest 编号），**不是 session seq，也不是 surface 下标**。
- 本仓库没有任何 block store / search_context / decompress 等价物。

### 2.2 与 DSH 的最小差异

| 方面 | DSH 宿主 | DeepPyro P1 建议 |
| --- | --- | --- |
| 工具归属 | 宿主上下文管理 | 仓库内工具，注册进 ToolsPlugin |
| block 存储 | 宿主 block store（可跨会话 search） | 每 session 一个 sidecar JSONL（不跨会话搜索） |
| refs | 宿主生成的 mNNNNN，稳定 | 自建 ref 表（见 2.3），首次使用时固化 seq |
| 检索 | search_context / decompress | P1 可选；不同批做则只有 checkpoint 文本 |
| 结果文本 | 固定格式（head/tail/节省量/compressible 提示） | 复刻同格式（便于对比与提示模型） |
| 落盘 | 无 session 事件 | user/message checkpoint（surfaceOp replace），复用 compaction 事务 |

### 2.3 Python 设计

**模块与职责**（建议写域）：

- `src/deeppyro/compact/blocks.py`：block/ref 存储与解析（纯逻辑 + 文件）。
- `src/deeppyro/compact/refs.py`：m 引用 <-> surface seq 映射。
- `src/deeppyro/compact/compress_tool.py`：`CompressTool(BaseTool)` 与事务。
- `src/deeppyro/compact/block_tools.py`：`SearchContextTool` / `DecompressTool`（可选 P1，见决策项）。
- 测试：`tests/compact/test_blocks.py`、`tests/compact/test_compress_tool.py`。
- 文档：`docs/spec/compact-blocks.md`（block/ref 格式）+ INTERFACES 修订。

**数据结构**：

    BlockRecord = {
      "id": "b1",                 # 每 session 递增
      "sessionId": "session-...",
      "range": {"start": "m00005", "end": "m00072"},
      "startSeq": 51, "endSeq": 118,      # 解析时固化的 surface seq
      "title": "Windows 崩溃诊断",          # 可选主题
      "summary": "...",                    # 完整摘要（不截断）
      "createdAt": 1794062030984
    }

- 文件布局：`<store_root>/blocks/<encode_segment(session_id)>.jsonl`，一行一条 BlockRecord，紧凑 JSON + \n，追加写 flush+fsync；`<store_root>/blocks/<encode_segment(session_id)>.refs.jsonl` 存 `{"ref":"m00005","seq":51,"messageId":"msg_...","assignedAt":...}`。
- 选择 `<store_root>/blocks/` 而不是 session 文件旁边，是为了同时支持扁平与嵌套布局、避免 `session.v4.jsonl.blocks.jsonl` 这类名字。

**refs 解析（最容易出错的点）**：

1. 输入解析：按行扫描 content；块头正则 `^m(\d{1,6})\s*[–—-]\s*m?(\d{1,6})(?:\s+(.+))?$`；块体为后续行直到下一个块头。
2. 每个 ref 必须能在 ref 表中解析到 seq：ref 表优先；未登记时按“当前 `derive_messages()` 的 1-based 序号”登记（m00001 = 第一条可见消息），固化 seq 后不再改变。
3. 校验：范围升序且互不重叠；start <= end；所有 seq 仍在 surface nodes 中且连续；范围不能包含 node0（system prompt 只能被单条 system/message 替换）；块体非空；同一调用内 ref 不得重复。
4. 失败返回 `ToolResult(is_error=True, text=<可操作错误>)`，不落任何事件（tool/call 已由 loop 记录，工具不得半写）。

**事务**（对齐 compact/compactor.py 的写法）：

    seqs = [resolved seq for start..end]                    # 连续 surface 节点
    start_seq, end_seq = seqs[0], seqs[-1]
    summary_event = session.append("compaction/summary", {   # 仅用于日志可审计
        "compactionId": cid, "summary": blocks_text,
        "shadowedRange": {"start": start_seq, "end": end_seq},
        "shadowedSeqs": seqs, "shadowedTokenCount": est,
    })
    checkpoint = Message(role="user", id=new_message_id(),
        source={"kind": "compact-checkpoint", "compactionId": cid},
        content=frame_summary(blocks_to_markdown(blocks)))
    session.append("user/message", checkpoint.model_dump(exclude_none=True), {
        "surfaceOp": {"op": "replace", "startSeq": start_seq, "endSeq": end_seq},
        "sourceEventSeqs": [start_seq, summary_event, *seqs],   # 注意去重/排序
    })

- `sourceEventSeqs` 必须严格递增且覆盖每个被 shadow 的节点；因此 summary 事件 seq 夹在区间之外时需按实际值排序，不能假设顺序。
- 建议先写 blocks/refs sidecar，再 append checkpoint；若 append 失败，回滚 sidecar（记录 offset 截断）。
- checkpoint 正文建议包含每个 block 的完整 summary（用 frame_summary 包 <compacted-summary>），这样即使没有 search/decompress 也不丢信息；block store 用于精确回溯。

**工具签名**：

    class CompressTool(BaseTool):
        name = "compress"
        description = <复刻宿主描述，去掉 billion-context 归属句>
        parallel_safe = False
        parameters = make_parameters({"content": {"type": "string", ...}}, required=["content"])
        async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult

**接线**：

- 需要 Session：建议 `runtime._plugin_services()` 增加 "sessions"，工具用 `ctx.services["sessions"]` + `ctx.session_id` 取 Session（或由 plugin setup 把当前 session 注册进 services）。
- 注册方式：新插件 `CompressPlugin.setup` 里 `ctx.service("tools").add(factory)`；factory 返回 CompressTool(BlockStore(root), ...)。
- 配置：`[compact]` 段可加 `blocksDir`、`compressEnabled`（默认 true）；不新增权限（只写自己的 sidecar 与 session 日志）。
- 事件类型：**不新增** session 记录类型（沿用 compaction/summary + user/message；KNOWN_EVENT_TYPES 已含 compaction/*）。

**测试策略**：

1. 解析：用 fixtures 中 11 条真实 `[Compressed ...]` 结果文本反向校验块头格式（块头在 result 里是 `b1 full summary ...`，不是原始 content；因此单测用人工样本 + 2 条 fixture result 做“输出文本格式快照”）。
2. 存储：追加/读取/ref 表冲突、损坏行、并发 append。
3. 事务：在 fixture session 副本上执行 compress，随后 `fold_surface` 正确、`derive_messages` 长度减少、checkpoint 位于原位置、严格校验通过；重复压缩同一范围被拒。
4. 失败：范围越界/含 node0/不连续/空块体 → is_error 且日志无新增事件。
5. 与 Compactor 兼容：同一会话先 /compact 再 compress 不出错（两条 checkpoint 互不干扰）。

### 2.4 实施拆分（compress）

| 任务 | 依赖 | 写域 | 量级 |
| --- | --- | --- | --- |
| A1 block/ref 存储 + 解析 | — | src/deeppyro/compact/blocks.py, refs.py; tests/compact/test_blocks.py | ~350 行 + 250 测试 |
| A2 compress 工具 + 事务 + 结果文本 | A1 | compact/compress_tool.py, builtin 接线, runtime services | ~450 + 350 |
| A3 search_context/decompress（可选 P1） | A2 | compact/block_tools.py | ~250 + 200 |
| A4 docs（compact-blocks.md）+ fixture 补充请求 | A2 | docs/spec/ | ~100 |

非目标：跨会话全局 block 搜索、与宿主 block store 的二进制兼容、block 过期/GC 策略（P1 只做显式 delete 接口或不做）。

---

## 3. Feature B：projection checkpoint

### 3.1 DSH 真实语义（dsh-session-projection）

包路径：D:/dshworkspace/_dsh_src/dsh-session-projection/lib/index.js；参考文档 _dshdocs/dsh-session-projection.md、dsh-session-projection-cache.md。

- 注册：`ProjectionDefinition{key, stateSchema, stateVersion, init(header, inheritedEventCount), apply(state,event), wire?:{viewSchema, view(state), summary?}}`；`register(definition)`（line 84）返回 handle，同 key 不同 stateVersion 直接抛错（line 129 校验 stateVersion 为安全非负整数）。
- apply 纪律：同步；无关事件必须返回**同一个 state 引用**（Object.is 闸门，避免无谓通知）；改变时返回完整新状态；事件绝不允许带裸增量。
- 读取：`snapshot(session, keys?)`（line 232）返回 `{asOfSeq: cursorBefore(session.seq), values}`，只含已注册且带 wire 的 key；`onChanged`（199）每个变化客户端单元一次，`onEventChanged`（212）每个已提交事件一次（携带 [{key,val}] + 致因 seq）。
- 持久：`checkpoint(session)`（line 317）返回 `{key: {ver: stateVersion, seq: cell.observedSeq, val}}`；`restoreFloor(checkpoint)`（line 346-355）取所有 ver 匹配行里 `min(row.seq + 1)`，再减一得 `SessionLogOffset`；restore 从该水位读取尾部并在存储后缀上重折叠，丢弃 ver 不匹配或越过日志末尾的行。
- 配套 cache（dsh-session-projection-cache）：持久化到 session_projcache domain，per-record 布局、write-behind 节流、cached listing；冷读可跳过全量日志。
- 限制：注册表进程级；cell 只在内存；checkpoint 成本 O(surface)；summary 投影排除 wire.summary=false 的 key。

**注意**：这不是 dsh-session-checkpoint-policy。后者的语义是“三个持久屏障”（llm/stream 前、顶层 tools/execute 正文前、agent/pre-step）+ fail-closed，我们已有等价物（append 内 flush+fsync；tool/call 在工具执行前已落盘），**不要重复实现**。

### 3.2 现状与最小差异

- P0 已有 ActivityService：`refresh(session)` 遍历全部 records 折叠 todo/jobs/agents/plan_mode，单例全局 state、subscribe 通知、payload 相同返回同一对象。它已经具备“单域投影 + 订阅”的雏形，但每次全量重放、无 per-session cell、无持久化、无法注册新 key。
- 最小差异：P1 只做“进程内通用注册表 + per-session 增量 cell + 可选 sidecar checkpoint/restore”，**不移植** stateSchema/viewSchema/客户端 wire 协议与 IPC 发布；activity 作为第一个消费者，但 TUI 切换可选。

### 3.3 Python 设计

模块：`src/deeppyro/projection/__init__.py`、`registry.py`、`checkpoint.py`；插件 `src/deeppyro/builtin/projections.py`（provide="projections"）；测试 `tests/projection/**`；文档 `docs/spec/projection.md`。

    @dataclass(frozen=True)
    class ProjectionDefinition:
        key: str
        state_version: int
        init: Callable[[SessionHeader, int], Any]      # inheritedEventCount
        apply: Callable[[Any, LogRecord], Any]
        wire: Callable[[Any], Any] | None = None
        summary: bool = True

    class ProjectionRegistry:
        def register(self, definition: ProjectionDefinition) -> ProjectionHandle
        def snapshot(self, session: Session, *, keys: Iterable[str] | None = None
                     ) -> dict[str, Any]                       # {asOfSeq, values} 的 values 部分
        def checkpoint(self, session: Session) -> dict[str, dict[str, Any]]
        def restore(self, session: Session, checkpoint: Mapping[str, Any]) -> None
        def on_changed(self, listener: Callable[[str, str, Any], None]) -> Callable[[], None]

    class ProjectionHandle:
        def state_of(self, session: Session) -> Any
        def reader_for(self, session: Session) -> Callable[[], Any]
        def dispose(self) -> None

实现要点：

- cell 键 `(session_id, key)`；`observed_seq` 记录已 apply 的最大 seq；新事件按 seq 递增只 apply 一次；`state_of` 首次调用从 header + inheritedEventCount init 后补放全部事件。
- inheritedEventCount：fork 会话以第一条 `session/end-seed` 的位置为切点（P0 fork.py:118-125 写入 data={inherited:true}）；含义是“继承了多少条事件”，供 projection init 决定是否需要从 fork 点重新初始化而不是从 0 重放。实现里按 `< end-seed.seq` 的事件数计数（trimmed 时用位置而非 seq 差）。
- 一致性：注册表持有所需 Session 只读引用；append 后调用 `on_event(session, record)`（或在 `Session.append` 观察者钩子中）推进 cell；P1 先提供显式 `notify(session, record)` 由 AgentSession/TUI 调用，避免改 session 模块。
- 持久格式：adjacent sidecar `<session 文件路径>.projections.json`（扁平与嵌套布局都成立），形状 `{"version":1,"cells":{key:{"ver":1,"seq":123,"val":...}}}`；写入 tmp+fsync+os.replace，write-behind（checkpoint() 或 close 时写，不每事件写）。
- restore：读 sidecar，ver 不匹配跳过；对每个 cell 从 `seq+1` 开始回放尾部记录；若 seq 越过日志末尾则整行丢弃（对齐 DSH restoreFloor 语义）；asOfSeq 取最后应用的事件 seq。
- fork：子会话没有 sidecar 时从 end-seed 切点 init；父的 sidecar 不复制（避免 stateVersion/切点错误）。
- 错误：sidecar 损坏 -> 视为无 checkpoint 并记录 warning（不阻塞读取）；stateVersion 冲突在 register 时抛 `ValueError`（或新 `ProjectionError`，需 Lead 决定是否加进 core.errors）。
- 配置与权限：`[projection]` 段可加 `checkpointEnabled`（默认 true）、`writeBehindMs`；不新增权限（只读同进程 session、只写自己的 sidecar）。
- 契约/接线点：只消费公开的 `Session.header` / `Session.records` / `LogRecord` / `SessionHeader`，不改 session 模块；插件 `provide="projections"`；与 `ActivityService` 的接入由 activity owner 决定（P1 可另注册等价 def 做对比，不动 activity.py）。

### 3.4 测试策略

1. 用 fixtures 的真实事件流注册“todos/jobs/team/plan_mode”四个 def，断言与 ActivityService.refresh 的结果逐字段相等（插桩对比）。
2. 无关事件返回同一对象；改变事件返回新对象；通知次数正确。
3. checkpoint -> 新进程式 restore -> 全量重放结果一致（含 trimmed 会话与含 9 次 replace 的会话）。
4. stateVersion 变化使旧 checkpoint 失效；越过日志末尾的 row 被丢弃；损坏 sidecar 不致命。
5. fork 会话（fixtures 里 session-66de8981 是 session-c423d78a 的 fork）用 end-seed 切点验证 init 入参。
6. 性能护栏：restore 只回放 checkpoint 之后的记录（计数 monkeypatch）。

### 3.5 实施拆分（projection）

| 任务 | 依赖 | 写域 | 量级 |
| --- | --- | --- | --- |
| B1 注册表 + cell + 通知 | — | src/deeppyro/projection/{__init__,registry}.py; tests/projection/** | ~500 + 400 |
| B2 checkpoint/restore sidecar | B1 | projection/checkpoint.py | ~300 + 250 |
| B3 builtin projections 插件（activity/stats/title 三 def） | B1,B2 | builtin/projections.py（activity 文件由其 owner 改） | ~300 + 200 |
| B4 docs + INTERFACES 登记 | B2 | docs/spec/projection.md | ~120 |

非目标：客户端 wire schema / IPC 推送、投影缓存加速 listing、把 TUI 全部改成订阅模型（P1 只提供能力 + 一个消费者试点）。

---

## 4. Feature C：session-query

### 4.1 DSH 真实语义（_dshdocs/dsh-session-query.md；源码不在 _dsh_src）

- 服务 `ctx.sessionQuery`，seam 在 dsh-session-query，具体后端 dsh-session-query-sqlite；本包只定义精确读取，全文搜索由后端实现。
- API：`listSessions()`（每个逻辑会话最新在前，带 live/persisted/formatStatus）、`readSession(id)`（回放校验的完整原始日志）、`filterSessions(filters)`、`filterEvents(id, filters)`、`readTitleSnapshots(ids)`、`listEvents(id)`（轻量逐事件）、`readSurface(id)`（完整当前模型 surface）、`readEvent(request)`（一个完整事件 + 有界前后窗口）、`traceSession(id)`（祖先链 + 递归后代树）、`traceEvent(request)`（位置替换者 + 被引用源事件）、`searchSessions/searchEvents`（全文分页）。
- 过滤器：session 按 id/cwd（可空）/createdAt 范围/parent/来源可用性；event 按 seq/时间/类型/surface/字面文本；数组 AND、子句内列表 OR、空列表不匹配、范围含端点；非法值 -> `SESSION_QUERY_INVALID_FILTER`；文本是字面、不区分大小写、空白灵活扫描（非全文）。
- 配置：`readWindowMax` 默认 50、`persistedReadConcurrency` 4、`preparedSessionCacheSize` 5。
- 错误码：SESSION_QUERY_SESSION_NOT_FOUND / SOURCE_CONFLICT（live 与持久 header 不一致）/ PERSISTENCE_FAILED / CORRUPT_SESSION / INVALID_SURFACE / INVALID_FILTER。
- 语义细节：live-first；返回脱库 clone；一次规范 surface fold（listEvents/readSurface/traceEvent 共用）；冷日志写入者中断时用内存 interruptedTurnClosers 补齐，读取从不改持久化；traceSession 一次语料遍历；traceEvent 沿位置替换者跟到最终节点，被引用源事件链接不传递；无调用方授权（可信基础设施）。
- 限制：精确读取会重放完整日志；文本扫描无排名；无搜索协调器/回退。

### 4.2 最小差异

| 方面 | DSH | DeepPyro P1 |
| --- | --- | --- |
| live 会话 | 进程级注册表，live-first | P0 无全局 live 注册表（TUI 只持有一个 Session）；P1 持久化优先，预留 `live_provider` 钩子 |
| 枚举 | 轻量 header/摘要 | 新增轻量 `list_summaries()`（read_header + tail 扫 title），不碰 `SessionStore.list` |
| 搜索 | SQLite FTS5 分页 | 非目标（P2）；只做字面过滤 |
| 事件文档 | 按类型语义提取文本 | 复用 `derive.event_message` + tool/call 的 name/arguments |
| 错误 | 专用错误码类 | 新 `SessionQueryError(SessionError)`，`code` 属性存 DSH 同名常量（不改 core.errors） |

### 4.3 Python 设计

模块：`src/deeppyro/session_query/{__init__,query.py,types.py,filters.py,tracing.py}`；测试 `tests/session_query/**`；文档 `docs/spec/session-query.md`。

    @dataclass(frozen=True)
    class SessionSummary:
        id: str; path: Path; cwd: str; createdAt: int
        title: str | None; parent: str | None; origin: str | None
        is_seeded: bool; delegation_depth: int; agent_preset: str | None

    @dataclass(frozen=True)
    class EventWindow:
        session_id: str; seq: int; event: LogRecord
        before: list[LogRecord]; after: list[LogRecord]

    @dataclass(frozen=True)
    class SessionTrace:
        ancestors: list[SessionSummary]; descendants: list[SessionSummary]

    class SessionQuery:
        def __init__(self, store: SessionStore, *,
                     read_window_max: int = 50,
                     live_provider: Callable[[str], Session | None] | None = None) -> None
        def list_summaries(self) -> list[SessionSummary]
        def get(self, session_id: str) -> Session
        def read_session(self, session_id: str) -> SessionRead
        def read_surface(self, session_id: str) -> list[Message]
        def list_events(self, session_id: str, *, types: Collection[str] | None = None,
                        surface: bool | None = None, after_seq: int = 0,
                        limit: int = 500) -> list[LogRecord]
        def read_event(self, session_id: str, seq: int, *,
                       before: int = 2, after: int = 2) -> EventWindow
        def filter_events(self, session_id: str, event_filter: EventFilter) -> list[LogRecord]
        def trace_session(self, session_id: str) -> SessionTrace
        def trace_event(self, session_id: str, seq: int) -> list[LogRecord]

实现要点：

- `list_summaries` 只读 header 行（`read_header`，一次 open + 读首行），再从文件尾部读最多 64KB 扫最后一条 `session/title` 与 `session/end-seed`；**不做**全量解码。性能测试用 monkeypatch `read_session_file` 断言调用次数为 0。
- `read_surface` = `Session.derive_messages()`（内部严格 fold；trimmed 自动 tolerant）。
- `trace_session`：对每个会话只读 header 建 id->parent 图（可缓存签名 mtime+size），返回祖先链和递归后代（先序、去环）。
- `trace_event`：一次事件流扫描得到 surface fold；若事件是 replace 目标区间内，找替换者并继续；被 replace 事件的 sourceEventSeqs 只展示一跳。
- `filter_events`：`EventFilter(seq_from, seq_to, types, surface_only, text)`；文本提取：消息类用 `Message.text() + reasoning()`，tool/call 用 name+arguments，session/title 用 title；非法（seq_from>seq_to、未知类型）抛 SessionQueryError。
- 错误映射：不存在 -> `SessionQueryError("SESSION_QUERY_SESSION_NOT_FOUND", ...)`；坏日志 -> CORRUPT_SESSION（保留 cause）；不合法 surface -> INVALID_SURFACE。
- 接线：新插件 `SessionQueryPlugin`（provide="sessionQuery"，inject=["sessions"]，构造 `SessionQuery(store)`）；TUI 的 `recent_sessions` 可切到 `list_summaries`（tui/runtime.py 由其 owner 改）。
- 配置与权限：`[sessionQuery]` 段可加 `readWindowMax`（默认 50）、`preparedSessionCacheSize`（默认 5）；只读、无调用方授权（与 DSH 一致）。
- 契约/接线点：复用公开的 `SessionStore` / `SessionInfo` / `Session` / `derive_messages` / `fold_surface` 与 layout/reader helpers，不改 P0 契约；新增公开 API 需写入 INTERFACES.md 下一修订。

### 4.4 测试策略

1. 30 个 fixtures 全部：`list_summaries` 数量/顺序/标题；`read_surface` 与 `derive_messages` 等价；`list_events` 计数与 seq 单调；每个事件 `read_event` 窗口边界正确。
2. 过滤：seq 范围含端点、类型 OR、surface、字面大小写/空白；非法过滤错误码。
3. trace：用 fixtures 里 2 个真实 parent 关系（session-66de8981 -> session-c423d78a；7fdb12e1 -> session-ddb1ba3d 但非 fork）验证祖先/后代；无父会话返回空链。
4. trace_event：用 ddb1ba3d 的 system replace（seq50 替换 seq8）与 8 个 developer replace 验证位置替换者；sourceEventSeqs 一跳。
5. 性能护栏：list_summaries 不得整文件解码（monkeypatch）；12MB zstd 会话读取时间上限（宽松阈值，CI 用 fixture 副本）。

### 4.5 实施拆分（session-query）

| 任务 | 依赖 | 写域 | 量级 |
| --- | --- | --- | --- |
| C1 SessionQuery 核心读取（get/read_session/read_surface/list_events/read_event） | — | src/deeppyro/session_query/**; tests/session_query/** | ~600 + 450 |
| C2 轻量 list_summaries + SessionQueryPlugin + TUI 接入 | C1 | session_query/**, builtin/session_query.py, tui/runtime.py | ~250 + 150 |
| C3 filters + trace_session/trace_event | C1 | session_query/filters.py, tracing.py | ~350 + 300 |
| C4 docs + INTERFACES 登记 | C1-C3 | docs/spec/session-query.md, docs/spec/INTERFACES.md（Lead 改） | ~120 |

非目标：SQLite/FTS5 全文搜索、live-first 的进程级会话注册表、跨会话聚合统计、持久化索引文件（P1 靠 header 扫描）。

---

## 5. Feature D：stream chunks

### 5.1 DSH 协议事实

- `StreamChunk` 精确联合（D:/dshworkspace/_dsh_src/dsh-llm/lib/typert.host.js:595-596）：
  - `{type:'block-start', index, blockType}`
  - `{type:'text-delta', index, text}`
  - `{type:'reasoning-delta', index, text}`
  - `{type:'tool-call-delta', index, id, name?, argumentsDelta}`
  - `{type:'block-end', index, block}`
  - `{type:'usage', usage}`
  - `{type:'finish', reason, replayState?}`
- ContentBlockMap（:319-320）：text / reasoning / image / file / tool-call / tool-addition / tool-removal。
- FinishReasonMap（:355-356）：stop / tool-calls / max-tokens / aborted{failure} / error{failure}。
- TokenUsage（:632）：inputTokens、outputTokens、totalTokens?、cacheReadTokens?、cacheWriteTokens?、reasoningTokens?。
- 顺序不变式：usage 先于 finish；tool 参数保持原始 JSON 字符串；finish 之后不再有内容；每个流恰好一个终止 finish。
- 持久化：AssistantStreamAccumulator（dsh-llm/lib/index.js:943 附近）把 token 分片压成 text-chunks/reasoning-chunks/tool-call-chunks/chunk 记录（time0/dt/texts/args）；读取器：expandAssistantStream（:1257）、isTokenDelta（:1312）、lastAssistantStreamChunk（:1411）、assistantStreamChunks（:1423）、assistantStreamFirstTokenTime、assistantStreamHasVisibleContent/Text、joinAssistantStreamText、assembleAssistantStream。
- 我们的 `agent/stream.py` 已实现同样的压缩记录与 expand（且额外限制同块合并 gap <= 60000ms、拒绝负 gap）。

### 5.2 缺口清单（逐项）

| 项 | DSH | 仓库现状 | 影响 |
| --- | --- | --- | --- |
| block 边界 | block-start/block-end 分片，独立 index | 无事件类，不落盘 | 无法表达多 text block / 交错 reasoning-text / 未来 image block |
| 分片 index | 每块独立 index | text/reasoning 恒为 0，tool-call 按首次出现顺序 | 多块内容会被错误合并成一个块；回放顺序可能错 |
| tool-call name | name 只在变化时出现（delta 语义） | 每个 args delta 都带 name | 记录冗余；name 缺失语义无法表达 |
| usage 字段 | cacheRead/cacheWrite/reasoning tokens | 只有 cached_input_tokens | 成本统计缺口（cacheWrite 是 DeepSeek 计费关键） |
| finish | reason 结构含 failure；有 replayState | kind 字符串 + failure | 语义可近似；replayState（断点重放）缺失 |
| 读取器 | firstTokenTime/hasVisibleText/joinText/lastChunk 等 | 只有 expand_stream | TUI/统计要自己重算；性能与一致性风险 |
| 适配器 | provider 直接产出 chunk | adapter 产出简化事件 | 需在 adapter 侧合成 index/block 事件才能向上保真 |

### 5.3 Python 设计（两个方案，需 Lead 决策）

**方案 A（推荐）：core 协议 additive 扩展**

    @dataclass(slots=True)
    class BlockStart: index: int; block_type: str
    @dataclass(slots=True)
    class BlockEnd: index: int; block: dict[str, Any]
    @dataclass(slots=True)
    class TextDelta: text: str; index: int = 0            # 新字段带默认值
    @dataclass(slots=True)
    class ReasoningDelta: text: str; index: int = 0
    @dataclass(slots=True)
    class ToolCallStart: call_id: str; name: str; index: int = 0
    @dataclass(slots=True)
    class ToolCallArgsDelta: call_id: str; delta: str
    #   建议新增 name: str | None = None; index: int = 0
    @dataclass(slots=True)
    class Usage:
        input_tokens: int = 0; output_tokens: int = 0; total_tokens: int = 0
        cached_input_tokens: int | None = None
        cache_write_tokens: int | None = None
        reasoning_tokens: int | None = None
    @dataclass(slots=True)
    class Finish:
        kind: FinishKind; failure: dict[str, Any] | None = None
        replay_state: dict[str, Any] | None = None
    StreamEvent = (TextDelta | ReasoningDelta | BlockStart | BlockEnd
                   | ToolCallStart | ToolCallArgsDelta | Usage | Finish)

- 兼容性：所有新字段都有默认值，现有 adapter 构造调用不变；`_consume_stream` 增加 BlockStart/BlockEnd 分支，text/reasoning 用事件 index，tool-call 记录 name 仅在变化时发。持久化格式不变（assistant/message.stream 仍是 accumulator 快照）。
- 配置与权限：无新增配置或权限；`StreamEvent` 属 INTERFACES §3 冻结契约，方案 A 的字段扩展需在下一修订登记（Lead/协议 owner 改 core/protocols.py）。
- FinishKind 现有 "length" 与 DSH "max-tokens" 不同：P1 不改现有取值（adapter 已映射），只在 session-query 的语义文档里标注；除非 Lead 要求统一，避免破坏所有 adapter 测试。
- 修改点：core/protocols.py（需 Lead 批准，冻结契约）、llm 适配器（可选增量，不要求一次全改）、agent/session.py `_consume_stream`、agent/stream.py。TUI 无需改（sink 帧结构不变）。

**方案 B（不批准 core 改动时）：agent 层内部增强**

- 在 `agent/stream.py` 定义内部 chunk 类型（与 DSH 同形），adapter 层（llm/）可选产出内部事件，loop 内部转换；不导出到 core.protocols。
- 能补 index/block-start/block-end 的落盘与 readers，但 adapter 与工具/插件生态看不到标准类型；P1 价值显著降低。
- 结论：若 P1 要做 stream chunks，建议批方案 A；否则只做 readers（低风险但收益有限）。

### 5.4 测试策略

1. fixture 回放：30 个会话的 assistant/message.stream 与 assistant/attempt.stream 全部用 expand_stream 展开，重建文本/工具调用与 message.content 对齐（P0 已有类似断言，补 block/index 断言）。
2. accumulator 合并：同 index 连续 delta 合并、不同 index 不合并、tool-call name 变化拆记录、gap 阈值行为（现有测试保留）。
3. 合成交错流：text(0) -> reasoning(1) -> text(0) -> tool-call(2) 展开后 block 顺序与内容；usage 在 finish 前；finish 后追加内容被拒。
4. readers：first_token_time/has_visible_text/join_text/last_chunk 对 fixtures 与合成流的期望值。
5. 回归：所有 adapter 现有测试不改构造调用仍通过（additive 默认值）。

### 5.5 实施拆分（stream）

| 任务 | 依赖 | 写域 | 量级 |
| --- | --- | --- | --- |
| D1 core StreamEvent additive 扩展 + agent 映射 | Lead 批准 | core/protocols.py, agent/session.py, agent/stream.py; tests/agent/**, tests/core/** | ~400 + 350 |
| D2 typed readers + 回放一致性 | D1 | agent/stream.py; tests/agent/test_stream.py | ~200 + 200 |
| D3 adapter 保真（openai/deepseek 等按需） | D1 | src/deeppyro/llm/** | ~200/适配器 |
| D4 docs/spec/stream.md | D2 | docs/spec/ | ~100 |

非目标：replayState/断点续传协议实现、把 FinishKind 改成 DSH 枚举、UI 可视化重做。

---

## 6. 总体拆分、依赖与风险

建议顺序（每项可独立交付）：

1. **C1 session-query 核心**（无依赖，低风险，立刻修 TUI 全量解码）
2. **C2 轻量枚举 + 插件接线**
3. **C3 filters + trace**
4. **B1 projection 注册表 → B2 checkpoint/restore → B3 builtin 投影**
5. **D1 stream 协议扩展（前提：Lead 批准）→ D2 readers**
6. **A1 block/ref 存储 → A2 compress 工具 → A3（可选）search/decompress**

跨写域冲突点（必须由 Lead 指定 owner 或串行化）：

- `src/deeppyro/core/protocols.py`（P0 冻结；只有 Lead/协议 owner 能改）
- `src/deeppyro/builtin/activity.py`（activity owner）
- `src/deeppyro/tui/runtime.py`（TUI owner；C2 需要加 sessions 到 ToolContext.services + 切换 recent_sessions）
- `src/deeppyro/llm/**` 适配器（llm owner；D1/D3）
- `docs/spec/INTERFACES.md`（Lead；新 API 登记）

量级估算（新增代码 + 测试，单人日 1.0 = 约 400-500 行含测试）：C 组 ~2.5、B 组 ~2.0、D 组 ~1.5-2.0、A 组 ~2.0-2.5；合计约 8-9 人日，不含联调与真实 fixture 补充。

主要风险：

- **fixtures 对 compress 无 ground truth**（最大风险）：我们没有一条真实 compress checkpoint 记录；格式推断若与 DSH 不同，未来导入/兼容会话会失败。缓解：先按本文档实现，Lead 协调补一份“压缩过的会话”fixture，或明确声明 DeepPyro 自有格式。
- **ref 坐标语义**：mNNNNN 与 seq 混用是最容易出 bug 的地方；必须集中实现并配不变量测试。
- **core 冻结**：D1 需要明确批准与 INTERFACES 修订；否则 stream chunks 的 P1 价值降级。
- **性能**：Store.list 全量解码存在于 TUI 启动路径；C2 前不要增加新的全量调用方。
- **trimmed fixtures**：严格 fold 对 trimmed 样本会失败；session-query/compress 测试需用 tolerant 模式或跳过被裁区间。

---

## 7. Lead 必读结论（7 条）

1. `compress` 工具不是 DSH compaction 的一部分，而是宿主 billion-context 的工具；它依赖**会话外 block store**，block 全文不在 session 日志里。照搬它必须先决定是否自建 block store + m 引用表，否则只能做“无损日志但不可检索”的半成品。
2. mNNNNN 与 session seq 是两套坐标系；P1 所有压缩/查询设计都要先显式定义 ref 映射并持久化，这是最容易在联调阶段返工的点。
3. projection checkpoint 与 checkpoint-policy 不要混淆：flush 屏障我们已经有了，P1 的价值在通用 per-session 增量投影 + 持久 rows + restore；ActivityService 可作为第一个消费者而不是重写目标。
4. session-query 是投入产出比最高的 feature，且可以完全不改 P0 契约（新包 + 新插件 + 一个 TUI 调用点），建议第一个做；全文搜索明确列 P2。
5. stream chunks 是唯一需要修改 core 冻结契约的 feature；建议批准“additive 扩展（新字段带默认值 + 两个新事件类）”，否则 P1 只能做 readers，无法表达多 block/交错分片。
6. fixtures 缺少任何压缩/checkpoint 记录，compress 的落盘格式目前是**推断设计**；需要在实施前补一份真实压缩会话 fixture（fixtures/ 只读，需 Lead 协调），或明确接受 DeepPyro 自有格式并写入 docs/spec。
7. 建议拆分与顺序：C1→C2→C3→B1→B2→B3→D1→D2→A1→A2（A3 视决策）；冲突集中在 core/protocols.py、builtin/activity.py、tui/runtime.py、llm 适配器，需要 Lead 指定写域 owner。

---

## 8. 未决问题（需 Lead 决策）

1. compress 的 block 全文是否进 P1（自建 store）？search_context/decompress 是否同批做？
2. block store 路径：`<store_root>/blocks/<session-id>.jsonl` 还是 session 文件旁 sidecar？是否允许多会话共享搜索？
3. refs 定义：以“当前 derive_messages 的 1-based 序号”登记，还是修改 UI 输出真实 mNNNNN（涉 TUI）？
4. 新公开 API（SessionQuery/ProjectionRegistry/SessionSummary）是否写入 INTERFACES.md 下一修订？由谁改？
5. 新错误类型是否进 core.errors（目前方案是在新包内子类化 SessionError，带 code 属性）？
6. stream 协议方案 A/B 与批准人；FinishKind 是否统一为 DSH 枚举。
7. 是否补真实压缩会话 fixture；若不能，是否接受“推断格式”并在文档标注兼容边界。
8. compression/query 的权限模型：DSH 是可信基础设施、无调用方授权；DeepPyro P1 是否沿用（工具只读同进程数据）。

---

## 附：证据索引（路径 + 行号）

- DSH 流式协议：D:/dshworkspace/_dsh_src/dsh-llm/lib/typert.host.js:319-320（ContentBlockMap）、:355-356（FinishReasonMap）、:595-596（StreamChunk）、:632（TokenUsage）
- DSH 流式压缩记录：dsh-llm/lib/index.js:943（AssistantStreamAccumulator）、:1257（expandAssistantStream）、:1312（isTokenDelta）、:1411（lastAssistantStreamChunk）、:1423（assistantStreamChunks）
- DSH projection：dsh-session-projection/lib/index.js:66（service）、:84-152（register）、:199（onChanged）、:212（onEventChanged）、:232-246（snapshot）、:317-329（checkpoint）、:346-355（restoreFloor）
- DSH checkpoint-policy：dsh-session-checkpoint-policy/lib/index.js:26-31（afterCheckpoint）、:33-48（aborted result）、:60-75（apply 的三个屏障）
- DSH format catalog：dsh-session-format-catalog/lib/index.js:40-69（catalog options/migrations v0-v4）、:71、:80-86（withChildren）、:91-111（historical）
- DSH token-meter：dsh-token-meter/lib/index.js:39（estimateContent）、:61-66（estimateSystemMessage）、:73-76（estimateMessage）、:83-86（estimateToolsTokens）、:199/394/449（三类投影）、:846-889（measure）
- session-query 文档：D:/dshworkspace/_dshdocs/dsh-session-query.md（API、过滤器、错误码、配置、源码地图）
- projection 文档：D:/dshworkspace/_dshdocs/dsh-session-projection.md、dsh-session-projection-cache.md
- compress 工具 schema/结果：fixtures/sessions/04bb4baa...jsonl seq179-180；fixtures/sessions/7fdb12e1-c6c4-4059-8a53-e7d9d566de7c.jsonl seq170-174（PTC）；11 条 `[Compressed` tool/result
- 本仓库 session：src/deeppyro/session/{store,reader,writer,derive,fork,layout,events}.py；docs/spec/session.md:1-247
- 本仓库 compact：src/deeppyro/compact/{compactor,prompts,surface,tokens}.py
- 本仓库 stream：src/deeppyro/agent/{session.py:660-711,737-796,1121-1158,1198-1209,stream.py}；core/protocols.py:50-86
- 接线：src/deeppyro/builtin/sessions.py:12-36；builtin/tools.py:15-118；builtin/activity.py:121-247,279；tui/runtime.py:112,294-310,601-603
