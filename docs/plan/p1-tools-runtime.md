# P1 工具与运行时特性调研（run_code / ask_user_question / working_directory / image offload）

> 状态：调研与设计文档，**不含实现改动**。仓库 D:\code\deeppyro，基线为当前工作树（其他 workstream 正在并行修改 agent/tui/bundles）。
> 作者：tools-dev；对应任务：task-31。
> 约束：本文只覆盖 4 个 P1 特性，不改现有包结构，优先复用已冻结的 core 契约与现有 service/plugin 接线点。

## 0. 证据等级与阅读方式

DSH 发行包位于 D:\dshworkspace\_dsh_src\<package>\，**包自身只有 README.md、package.json 与编译后的 lib/*.js，没有 src/*.ts**（dsh-working-directory 下能搜到的 332 个 .ts 全部来自嵌套 node_modules 依赖，不是该包源码）。因此：

- “真实语义”优先从编译 JS 与 typert 类型声明中逐行确认；本文给出的行号都是 lib 文件行号，README 只作交叉验证。
- 上游包 dsh-tool-executor-ptc、experimental/ptc-runtime-python 在本机 _dsh_src 中不存在（dsh-tools/README.md:132、dsh-ptc-runtime/README.md:45 只给了引用）。run_code 的精确 model-facing schema 无法从发行包取得，本文明确标注“推导/需 Lead 决策”。
- fixtures 只有 DSH 快照，不含 run_code；fixtures/records/by-type/request_header.jsonl 的 data.header.tools 共 66 个工具，确认**没有** run_code。30 个 fixtures/sessions/*.jsonl 中也没有 request_header 含 run_code（扫描脚本 D:\dshworkspace\_scratch_tools\scan_run_code.py 输出 NOT FOUND）。
- 现有 Python 仓库已实现 tools/sandbox/jobs/hooks 等；本文的接线点以 docs/spec/INTERFACES.md 的真实名字为准：tools、prompt、agents、sandbox、sessions、activity、jobs、settings、inspect（INTERFACES.md:333-357）。

四个特性的推荐优先级：

| 特性 | 建议 | 主要理由 |
|---|---|---|
| working_directory | P1 做，最先做 | 语义清晰、无新的外部依赖，现有记录类型与 runtime context 已就位 |
| ask_user_question | P1 做，与 working_directory 并行 | blocking 模式可完全对齐 fixtures，TUI 只缺一个输入弹窗 |
| image offload | P1 做第一阶段（附件存储 + 预算 + offload 记录/投影），第二阶段做 provider 接线 | 需要 core/session/llm 三处协作，风险集中在投影解释器与 provider 预算 |
| run_code | 建议 P2；若必须进 P1，先做 0.5-1 天 spike 再定 | 需要代码执行边界、嵌套工具调度、输出/超时/取消协议；Python 版无法直接照搬 Node 的 V8/进程 worker |

---

## 1. run_code（PTC / programmatic tool calling）

### 1.1 DSH 对应包与关键文件

- **抽象执行 seam**：D:\dshworkspace\_dsh_src\dsh-ptc-runtime\README.md
  - :28 README 明确“消费 ctx.ptcRuntime 或实现 backend”。
  - :32 resolve(request) → run(spec)；programFormat 为 async function body 或 REPL/Script；result.value / result.logs / result.error 分离。
  - :35-39 resolve/run 的调用示例。
  - :45 README 提到私有 experimental/ptc-runtime-python provider 在 fresh CPython subprocess 执行、无文件 confinement；本机不存在该目录。
  - :47 stateOwner 只为保留式 provider（Node in-memory / vm-script persistence）服务；同 owner 的调用不得重叠。
  - :55 失败 kind：exception / timeout / abort / worker-exit / invalid-output / output-limit / protocol / sandbox-unavailable；result.sandbox 独立报告 enforcement。
  - :73 timeout 描述符 {defaultMs,maxMs}；省略取默认、数字经校验并 cap、null 表示无截止；**run_code 只接受正数**。
  - :75 executionInstructions 是 provider 提供的使用说明，consumer 拼进 run_code schema。
  - :83 PtcRunRequest / PtcRunSpec / PtcRunResult 的字段说明。
  - :87 binding-global 与 error-class 名必须是 [A-Za-z_][A-Za-z0-9_]*，并排除 PORTABLE_RESERVED_WORDS（ECMAScript ∪ Python 保留字）、RESERVED_BINDING_GLOBALS（如 console、__dsh_main__）、RESERVED_ERROR_MEMBERS、DUNDER_MEMBER。
  - :129 run() 是一次性；logs 只在 resolved result 中返回，**没有 streaming logs/progress API**。
  - :131 各 provider 的 confinement 能力不同；Node provider 执行 resolved file policy，experimental Python provider 拒绝显式 policy，没有 container provider。
- **Node 默认 provider 与常量**：D:\dshworkspace\_dsh_src\dsh-ptc-runtime-node\README.md
  - :37-44 默认配置：executionMode: per-call、timeoutMs: 120000、maxTimeoutMs: 600000、maxOutputBytes: 67108864、maxOldGenerationSizeMb: 512、maxMessageBytes: 134217728、maxPendingCalls: 128、graceMs: 3000。
  - :49-57 配置表与语义（timeout 含嵌套工具/审批等待；maxOutputBytes 是 logs+completion/diagnostic 的合计预算；maxPendingCalls 是同时在途 host binding 调用的上限）。
  - :66 per-call = fresh Node process、async function body、top-level await/return、只接受 erasable TS；成功 result.value 为 lossless JSON，logs 为捕获文本；result.sandbox 独立报告 mode/denial/full|partial。
  - :70 vm-script = node:vm Script，支持顶层声明/await，不支持顶层 return 与 static import/export，await 最后一个表达式；**context 不是安全边界**，Node 访问仍受 process sandbox 约束。
  - :72 scriptPersistence 默认 false；true 时按 owner 保留 context。
  - :84-92 persistent-in-memory 按 Agent 保留 REPL/进程/变量；重启/超时/取消/进程退出清空；每次 evaluation 重置 cwd，process.chdir 不持久。
  - :106 超时覆盖 setup+execution+嵌套工具/审批；timeout 或取消通过 managed process owner 停止同步循环；成功结束也清理进程范围；timer 在 outcome 选定后停止，cleanup 可能超过 deadline。
  - :130 per-call 完成后未完成的 binding waits 会保持 pending，但停止 binding 调用与输出通知；child 保持 control pipe 直到 host 收到结果。
- **工具注册/保留名/模型面**：D:\dshworkspace\_dsh_src\dsh-tools
  - lib/types/contracts.js:4：export const RUN_CODE_NAME = 'run_code';
  - lib/index.js:440：注册时抛 tool name "run_code" is reserved for the PTC mode presentation transport and cannot be registered or shadowed；lib/index.js:463 restrict 也不能点名 run_code。
  - README.md:130-132：PTC mode 由 tool-executor-ptc 拥有 program transport、SDK 生成、nested dispatch、image context。
  - README.md:179：模型面结果 = outer program 的 printed lines + return value；都空时 (run_code completed with no output)；失败 Error: code run failed (<kind>): <message>，其后可能接 Captured output: 与 captured lines；inner dispatch events 只进 log；成功且带 image 的 sub-result 作为 source-attributed context 追加在 outer result 后。
  - README.md:128：未知/throwing tool 变成 structured error（UNKNOWN_TOOL），不结束 turn；取消是协作式（exec.signal）。
- **嵌套派发事件形状**：D:\dshworkspace\_dsh_src\dsh-commands\lib\typert.host.js
  - :644-646 PtcDispatchEventData extends PtcDispatchStartEventData { isError: boolean; content: ContentBlock[]; meta?: JsonValue; error?: {name: string; code: string; reason?: string;} }
  - :648-650 PtcDispatchStartEventData { rootCallId: ToolCallId; parentCallId: ToolCallId; subCallId: ToolCallId; name: string; arguments: unknown; }
- **已知的 DSH 侧约束**：run_code 是 registry 保留名（不能注册/遮蔽）；它不是一个普通工具，而是 PTC transport；子调用带 parent token 走同一个 ToolExecutionPipeline，并成对记录 dispatch-start / dispatch（.team-reports/dsh-explore/03-turn-and-tools.md:101-103）；并行池上限 maxParallelToolCalls 默认 10，提交严格按模型顺序（同文 :99）。
- **缺失上游**：D:\dshworkspace\_dsh_src 没有 dsh-tool-executor-ptc，也没有 experimental/ptc-runtime-python。run_code 的 schema、SDK 形态、image context 注入、per-call timeout 的 consumer 实现均无法逐行确认。

### 1.2 真实语义（确认过的部分）与关键常量

1. **run_code 不是普通工具**：模型可见的 run_code 是 PTC transport；普通工具在 PTC 模式下被转换成 SDK binding（DSH 由 dsh-tool-executor-ptc 生成）。普通 registry 不能注册同名工具（dsh-tools/lib/index.js:440）。
2. **执行单位是一次 program**：输入是 program 文本；输出是 lossless-JSON completion（value）+ 捕获的 logs；失败是结构化 error kind。没有 streaming 回调、没有中途进度 API（dsh-ptc-runtime/README.md:129）。
3. **programFormat**：per-call 下是 async function body（可 top-level await/return）；vm-script 下是 Script（顶层声明/await，最后一个表达式作为完成值）。Python 版需要明确选择一种。
4. **超时**：默认 120s、上限 600s；覆盖程序启动、执行、嵌套工具调用与审批等待；不是 CPU 计量（dsh-ptc-runtime-node/README.md:51、:106）。
5. **输出预算**：maxOutputBytes=67108864，是 serialized logs + completion/diagnostic 的合计预算；超出属于 output-limit 失败（README.md:40、:55）。
6. **并发**：maxPendingCalls=128 限制同时在途的 host binding 调用；maxParallelToolCalls 默认 10（dsh 文档），但 Python pipeline 当前 max_concurrency=8，需要按 consumer 语义决定是否在内层另设上限。
7. **binding 命名**：必须语言可移植，排除 ECMAScript ∪ Python 保留字、console 等后端全局、错误成员名与 dunder 成员（dsh-ptc-runtime/README.md:87）。
8. **失败 kind 是正交维度**：timeout 不等于进程退出；sandbox-unavailable 是 confinement 无法建立；invalid-output 是 completion 不符合 lossless JSON 契约；output-limit 是预算耗尽。
9. **记录形状**：outer call 仍是普通 tool/call + tool/result；inner 调用成对写 tool/ptc-dispatch-start 与 tool/ptc-dispatch（定义见 1.1）；inner 事件只进日志，不成为模型可见消息。带 image 的成功 sub-result 需要作为 source-attributed context 追加在 outer result 之后（这一条依赖 image 能力，见第 4 节）。

### 1.3 与 DSH 的最小差异（Python 版现实）

| 维度 | DSH | Python 版最小差异 |
|---|---|---|
| 语言/运行时 | Node/V8 + TypeScript（erasable） | 只能是 Python；没有 V8，也不应假装支持 TS |
| 隔离 | Node provider 受 managed process + resolved file sandbox；vm-script 明确不是安全边界 | 现有 sandbox 是**逻辑 gate**，不是 OS 隔离；Python 子进程也不是安全边界 |
| 进程模型 | 常驻 worker / managed process、control pipe、pending calls | 可以每次 fresh CPython subprocess，简单但每次启动开销大；常驻 worker 需要自建协议 |
| binding | 同步外观的 SDK，由 control channel 代理到 host | asyncio 下要么让程序 await tools.read(...)，要么用线程桥接；前者会改变 program 写法 |
| state | per-call 默认无状态；vm-script/persistent 可选 | P1 建议只做 per-call，无持久状态 |
| timeout/取消 | managed process owner 能停止同步循环 | subprocess 只能在 await 点取消；需要 kill + communicate 超时兜底 |
| schema | 由 consumer 拼 executionInstructions，精确 schema 未公开 | 只能推导，必须标“需 Lead 决策” |
| 输出 | printed lines + return value；image context | 可做 return value + captured stdout，但 image context 依赖第 4 节 |

### 1.4 Python 设计

#### 方案 A（推荐）：P1 不实现真实执行，只铺 transport 接缝

理由：
- Python 代码执行无法复用 Node 的 worker/managed process 语义；直接 exec() 是任意代码执行，且当前 PermissionGate 不是 OS 沙箱。
- 若用 subprocess，则 binding（工具调用）必须通过 stdio RPC 回 host，涉及并发、取消、审批、输出记账、schema 生成，工作量与风险都远超其他三个 P1 特性。
- DSH 自己也没有把 Python provider 放进发行包（README 只说 private experimental），说明它是实验性能力，不适合作为 Python 重写的 P1 门槛。

建议的“接缝”改动（不改行为）：
- 在 src/deeppyro/tools/registry.py 增加 RESERVED_TOOL_NAMES = frozenset({"run_code"})，register() 对保留名抛 ToolError（文案对齐 DSH：tool name "run_code" is reserved for the PTC mode presentation transport and cannot be registered or shadowed）。**需 Lead 决策**：这会改变现有插件注册行为。
- 在 src/deeppyro/tools/pipeline.py 预留 ToolDispatchHook 协议（当前已有 hooks；不要为 PTC 另造一套），让未来 transport 能在 pre-execute 之前接管 run_code；P1 不注册 run_code。
- 文档/提示词中不出现 run_code，模型不会看到该工具；与 DSH 默认（无 PTC consumer 时 run_code 不存在）一致。

#### 方案 B（若 Lead 要求在 P1 跑通）：最小 Python PTC

模块（全部新增，写域独立）：

    src/deeppyro/ptc/__init__.py
    src/deeppyro/ptc/types.py
    src/deeppyro/ptc/runtime.py
    src/deeppyro/ptc/worker.py
    src/deeppyro/ptc/tool.py
    src/deeppyro/builtin/ptc.py

类型（src/deeppyro/ptc/types.py）：

    PtcFailureKind = Literal[
        "exception", "timeout", "abort", "worker-exit",
        "invalid-output", "output-limit", "protocol", "sandbox-unavailable",
    ]

    @dataclass(frozen=True, slots=True)
    class PtcFailure:
        kind: PtcFailureKind
        message: str

    @dataclass(frozen=True, slots=True)
    class PtcRunSpec:
        program: str
        cwd: Path
        timeout_ms: int = 120_000
        max_output_bytes: int = 67_108_864
        sandbox_mode: str = "workspace-write"

    @dataclass(frozen=True, slots=True)
    class PtcRunResult:
        value: Any = None
        logs: tuple[str, ...] = ()
        failure: PtcFailure | None = None
        sandbox: dict[str, Any] = field(default_factory=dict)

服务（src/deeppyro/ptc/runtime.py）：

    class PtcRuntime(Service):
        provide = "ptc"
        inject = ["tools", "sandbox"]

        @property
        def timeout(self) -> dict[str, int]:
            """Return {"defaultMs": 120000, "maxMs": 600000}."""

        def resolve(self, request: Mapping[str, Any] | None = None) -> PtcRunSpec:
            """Validate cwd/timeout/sandbox; apply defaults and cap timeout."""

        async def run(
            self,
            spec: PtcRunSpec,
            *,
            bindings: Mapping[str, Callable[..., Awaitable[Any]]],
            ctx: ToolContext,
        ) -> PtcRunResult:
            """Execute one program; never raise for program failure."""

- run 的失败一律转 PtcRunResult.failure，不向 pipeline 抛异常；CancelledError 只用于 host 取消。
- worker 读 src/deeppyro/ptc/worker.py：fresh sys.executable -I -S -c 或临时代码文件；只允许 builtins 白名单；用一行 JSON 协议传 program、bindings 列表、cwd；stdout 只承载协议帧，用户 print 走 stderr 或 JSON logs 帧。**这不是安全边界**，必须在 meta 与文档中明确。
- binding 调用：worker 发出 {"type":"call","id":..., "name":..., "arguments":...}；host 用现有 run_tool_batch 或 registry 直接 dispatch，回 {"type":"result",...}；只能串行（P1），并发上限不需要 maxPendingCalls 的等价物。**需 Lead 决策**：是否允许 binding 并发；若要，复用 Semaphore。
- 程序形态：P1 只支持 programFormat=script（顺序语句 + 最后一个表达式），不支持 async function body；避免 Python 的 return 语义与顶层 await 兼容问题。
- timeout：asyncio.wait_for + 到点 process.kill()；grace_ms=3000 用于 drain stdout；超时失败 kind=timeout，文本含 code run failed (timeout): ...。
- 输出：捕获 stdout logs 与最终表达式值；序列化合计超过 max_output_bytes 时截断并返回 output-limit；对外只呈现 outer result。
- 失败映射：worker 非零/信号 = worker-exit；JSON 协议坏 = protocol；值不能 JSON 序列化 = invalid-output；未捕获异常 = exception；host 取消 = abort。

工具（src/deeppyro/ptc/tool.py）：

    class RunCodeTool(BaseTool):
        name = "run_code"
        description = "<由 PtcRuntime.execution_instructions 拼装>"
        parameters = {
            "type": "object",
            "properties": {
                "program": {"type": "string", "description": "..."},
                "timeoutMs": {"type": "number", "description": "..."},
            },
            "required": ["program"],
            "additionalProperties": False,
        }
        parallel_safe = False

- schema 是**推导值**：program 必填；timeoutMs 为正数、默认 120000、上限 600000；不暴露 sandbox_permissions（PTC 默认沿用当前 preset，需 Lead 决策是否允许一次升级）。
- 注册方式：不作为普通工具注册；由 PtcPlugin 在检测到 ptc 服务且配置开启时，通过 tools pipeline 的 transport slot 注入。若做不到，则只能像普通工具一样注册（但这违反 DSH 保留名语义，需要 Lead 明确接受偏离）。
- 模型面文本：成功时 "\n".join(logs) + return value 的 JSON；都空则 (run_code completed with no output)；失败 Error: code run failed (<kind>): <message>，有 captured output 时追加 Captured output:\n<lines>。与 DSH 的 String 文案保持一致。
- 记录：outer tool/call/tool/result 由 pipeline 写；每个 inner dispatch 写 tool/ptc-dispatch-start（rootCallId=outer call id，parentCallId=当前调用 id，subCallId=新 id，name，arguments 为对象）与 tool/ptc-dispatch（追加 isError、content 为 [{"type":"text","text":...}]、meta、error）。这两个 type 已在 core.wire.RECORD_TYPES（wire.py:202-203），不需要改 core。
- 配置（PtcPlugin.Config）：

    [ptc]
    enabled = false
    executionMode = "per-call"   # 只支持 per-call / script
    timeoutMs = 120000
    maxTimeoutMs = 600000
    maxOutputBytes = 67108864
    maxPendingCalls = 128
    graceMs = 3000

- 权限/审批：run_code 本身是否算“执行命令”需要明确。建议：read-only preset 直接拒绝（PermissionDenied("run_code is not allowed under read-only preset")）；workspace-write 允许，但 inner tool 调用仍逐个走 PermissionGate（因此 write 到 workspace 外仍会触发审批）；danger-full-access 全放行。run_code 自身的审批不应“一次批准整个程序”，否则 inner gate 形同虚设。**需 Lead 决策**：是否让 run_code 走 check_command（推荐）还是独立的 check_program。

### 1.5 测试策略（方案 B 才需要）

- tests/ptc/test_runtime.py：timeout（sleep 超限）、异常映射、invalid-output、output-limit、worker-exit、cwd 生效、timeout cap。
- tests/ptc/test_bindings.py：binding 成功/失败、未知工具、并发上限、取消传播、嵌套审批（fake gate）。
- tests/ptc/test_tool.py：schema 必填/额外字段、成功文本、空输出文本、失败文本、parallel_safe is False。
- tests/ptc/test_dispatch_records.py：成对 start/dispatch 记录、rootCallId/parentCallId/subCallId、arguments 是对象、inner result 不进 surface。
- fixtures：无 run_code 样本，只能自造；schema 回归只能断言“与推导 schema 一致”，不能声称对齐 DSH。

### 1.6 风险与非目标

- **最大风险**：把 Python exec/subprocess 当成安全沙箱。当前 sandbox 是逻辑 gate，Windows 上没有可靠的 OS confinement；必须在文档和工具描述中写明“不是安全边界”，并让 Lead 决定是否允许该能力默认关闭。
- 嵌套工具调用需要审批时，外层 run_code 的 timeout 是否覆盖审批等待（DSH 是覆盖）；Python 实现若把审批放在 host await 中，需要明确计时起点。
- 非目标：vm-script、persistent REPL、stateOwner、streaming logs/progress、SDK 静态类型生成、image context 注入、Node 兼容的 import/require。

---

## 2. ask_user_question

### 2.1 DSH 对应包与关键文件

- **工具实现**：D:\dshworkspace\_dsh_src\dsh-tool-ask-user\lib\index.js
  - :229-321 legacy（blocking）schema：questions 数组，item 必填 id/question，可选 header/options/multi_select；hidden output schema 为 {answers:[{id, selected, custom?}]}；:299-302 render = JSON.stringify(value) 的单个 text block；:304-320 execute 调 ctx.userQuestions.ask({questions, agent?, signal})。
  - :78-198 timed schema：questions 同 legacy，额外 timeout（integer，允许 -1 或 1..2147483 秒；描述“默认 120；除非用户指定时长否则省略；只有必须得到答案才能继续时才用 -1”）。
  - :8-10 validateTimeout：timeout !== -1 && (!Number.isInteger(timeout) || timeout < 1 || timeout > 2147483) → timeout must be -1 or a positive integer up to 2147483 seconds。
  - :23-27 pendingNotice 文本：No answer batch arrived before the timeout. This is pending, not a skipped answer. Continue useful independent work. The user can still answer; their reply will be a user message identified as answer_to_pending_question with this callId and the original questions. Do not treat this as permission.
  - :205-220 插件 Config：mode: union(legacy, timed) 默认 legacy；timeout: union(-1, number 1..2147483) 默认 120；inject = ["tools", "userQuestions"]；name = "tool-ask-user"。
  - :240-303 timed 的 hidden output schema：oneOf pending（{pending:true, callId, message}）与 answers（{answers:[...]}）。
  - :304-320 legacy execute 的 answers 映射：{id, selected: [...], custom?}（无 custom 时不写该键）。
- **审批 seam 的区别**：D:\dshworkspace\_dsh_src\dsh-user-approval\README.md
  - approval 是一次性权限判定，policy 只有 ask/never，answerer 缺失/异常时 fail-closed；记录 approval/asked 与 approval/decided，模型只看到工具结果和 runtime context policy，不看到审批 UI。
  - ask_user_question 是“问题/答案”，**不授予权限**；跳过不是许可；timeout 也不是 approval。
- **fixtures 成功/失败样本**（D:\code\deeppyro\fixtures\sessions\*.jsonl，共 8 次调用）：
  - 成功结果文本是单个 JSON 字符串：{"answers":[{"id":"proceed","selected":["开始排查（只读）(Recommended)"]},{"id":"symptom","selected":["记不清/没看到"]}]}
  - 多选 + 自定义：{"answers":[{"id":"next_action","selected":[],"custom":"能把D盘空间分配给C盘一点了吗"}]}
  - 失败样本：isError=true，text Error: dsh-im interaction adapter was disposed。
  - 所有调用都没有 timeout 字段（都是 legacy/blocking schema）。

### 2.2 真实语义

- 一个调用可含多个 question；questions 必填且非空；每个 question 的 id 在**本次调用内**唯一；question 必填非空。
- header 可选（短标题）；options 可选，每项 label 必填、description 可选；multi_select 默认 false。
- 推荐选项由模型排在第一位并在 label 末尾带 (Recommended)；工具不负责排序/改写。
- 答案按 id 回显；每个 answer 有 selected 数组与可选 custom；跳过的 item 是 selected: [] 且没有 custom。
- 模型可见结果只有一个 text block，内容是 JSON.stringify 的 answers 对象；不是 markdown，不是多 part。
- 无 timeout 的 legacy 模式是**阻塞**的：没有 answerer 就报错；timed 模式在 deadline 到达时返回 pending（问题仍可稍后回答，答案作为 user message 回来，kind=answer_to_pending_question）。
- 取消/非 live runtime root 报错；live child 拒绝 DELEGATED_CALLER，child 必须把未决问题放进 final result（README:72-74）。这属于 subagent 集成，P1 可不做。
- 参数校验错误（如 id 重复、空 question）应以工具错误结果返回，不结束 turn。
- 与 approval 完全独立：不读取 approvalPolicy，不调用 PermissionGate.ask，不产生 approval/asked/approval/decided。

### 2.3 与 DSH 的最小差异

- P1 只做 **blocking legacy**：不做 timed/pending、不做 answer_to_pending_question 后续消息、不做 IM adapter、不做 child delegation 拒绝逻辑。这能覆盖 fixtures 全部 8 条真实调用。
- TUI 需要新增问题弹窗；headless 明确返回错误结果（无 answerer）。
- 多问题一次弹窗顺序回答（DSH 行为）；答案结构保持一致。
- 不新增记录类型；只写现有 tool/call、tool/result。timed/pending 未来若做，才需要设计 pending 状态记录。

### 2.4 Python 设计

模块：

    src/deeppyro/user_questions/__init__.py
    src/deeppyro/user_questions/types.py
    src/deeppyro/user_questions/service.py
    src/deeppyro/builtin/user_questions.py
    src/deeppyro/tools/builtin/ask_user.py
    src/deeppyro/tui/screens/question.py   # 或并入 tui/screens.py

类型（types.py）：

    @dataclass(frozen=True, slots=True)
    class QuestionOption:
        label: str
        description: str | None = None

    @dataclass(frozen=True, slots=True)
    class Question:
        id: str
        question: str
        header: str | None = None
        options: tuple[QuestionOption, ...] = ()
        multi_select: bool = False

    @dataclass(frozen=True, slots=True)
    class Answer:
        id: str
        selected: tuple[str, ...] = ()
        custom: str | None = None

    @dataclass(frozen=True, slots=True)
    class AskRequest:
        questions: tuple[Question, ...]
        call_id: str

    AskHandler = Callable[[AskRequest], Awaitable[list[Answer]]]

服务（service.py）：

    class UserQuestionsService(Service):
        provide = "user_questions"
        inject = ["tools"]

        def configure(self, handler: AskHandler | None) -> None:
            """Install the host answerer (TUI); None means no answerer."""

        async def ask(
            self,
            questions: Sequence[Question],
            *,
            call_id: str,
            cancel: asyncio.Event | None = None,
        ) -> dict[str, Any]:
            """Return {"answers": [...]}; raise ToolError when unavailable/cancelled."""

实现要点：
- 校验：questions 非空、id 唯一且非空、question 非空、options 的 label 非空、multi_select 为 bool；错误抛 ToolError（由 pipeline 转 Error: ...）。
- handler 为 None：抛 ToolError("ask_user_question is not available in this session")。
- 取消：在 cancel 上 wait() 与 handler task 做 asyncio.wait(..., return_when=FIRST_COMPLETED)；取消时取消 handler 并抛 ToolError("question was cancelled")。
- handler 异常：包装成 ToolError(f"failed to ask the user: {exc}")；不把内部栈泄露到模型文本。
- 结果：Answer 转 dict 时，custom 为 None 则省略；selected 始终是 list（可为空），与 fixtures 一致。
- 并发：同一服务同一时间只允许一个 ask 在途（TUI 只有一个输入焦点）；用 asyncio.Lock 串行化，第二个调用等待或在有在途问题时返回错误。**需 Lead 决策**：等待还是立即失败；建议等待，但 pipeline 的 parallel_safe=False 已让工具串行，服务层 lock 只作兜底。

工具（tools/builtin/ask_user.py）：

    class AskUserQuestionTool(BaseTool):
        name = "ask_user_question"
        description = (
            "Ask the user a concise question when you need confirmation, a choice, or missing "
            "information before proceeding. An answer item with empty selected and no custom means "
            "the user skipped that question: continue without it, do not ask it again, and do not "
            "treat the skip as permission."
        )
        parameters = {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "id": {"type": "string", "description": "Stable id for this question; echoed in the answer."},
                            "question": {"type": "string", "description": "The specific question to ask the user."},
                            "header": {"type": "string", "description": "Optional short heading for the question, such as \"Confirm\" or \"Choose Mode\"."},
                            "options": {
                                "type": "array",
                                "description": "Optional choices to show the user. If you recommend one, put it first and append \"(Recommended)\" to that label.",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": True,
                                    "properties": {
                                        "label": {"type": "string", "description": "Short user-facing option label."},
                                        "description": {"type": "string", "description": "One sentence explaining the tradeoff or impact."},
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multi_select": {"type": "boolean", "description": "Whether the user may select more than one option. Defaults to false."},
                        },
                        "required": ["id", "question"],
                    },
                }
            },
            "required": ["questions"],
        }
        parallel_safe = False

- run 取 ctx.services.get("user_questions")；缺失时 ToolError("ask_user_question is not available in this session")。
- 结果：ToolResult(text=json.dumps({"answers": answers}, ensure_ascii=False), meta={"callId": ctx.call_id, "answers": answers})。文本必须与 fixtures 一样是单 JSON 字符串。
- 不写 ctx.log 自定义记录；tool/call/result 由 pipeline 写。如果未来 timed/pending，再设计 pending 记录。

TUI 接线：
- TuiRuntime.boot（src/deeppyro/tui/runtime.py:100-163）创建服务后调用 configure(handler)；services 字典加入 "user_questions"。
- TuiApp 增加 ask_question(request) -> list[Answer]，用 push_screen_wait(QuestionScreen(...))；多问题逐页/单页分组；multi_select=true 用 checkbox，否则 radio；支持“跳过”返回空 selected。
- TuiApp 的 ToolContext.cancel 在用户按 Esc/中断时触发，service 取消 handler。
- 现有 ask_approval（tui/app.py:315-328）不改造；问题弹窗与 approval modal 是两个 screen，互不复用，避免语义混淆。

Headless 接线：
- 不配置 handler；工具调用返回 Error: ask_user_question is not available in this session。这是显式、可预期行为，不阻塞 turn。
- HeadlessRuntime._plugin_services()（src/deeppyro/headless.py:370-391）加入 "user_questions"，这样工具能拿到服务并得到统一错误；也可以不注入，让工具报同样的错误。

配置：

    [plugins.tool-ask-user]
    mode = "blocking"   # P1 只接受 blocking；timed 预留但未实现

- 用 Config 的 extra="allow" 承载，避免改 config.py；若 Lead 希望强类型，再在 AgentConfig 增加字段（需改 config.py，非本任务写域）。
- 不做 timeout 配置；blocking 调用由用户交互自然结束。

权限/审批：
- 不需要 PermissionGate 检查；read-only preset 也允许提问。
- 不产生 approval 记录；不改变 approvalPolicy。

### 2.5 测试策略

- tests/user_questions/test_service.py：问题校验（空/重复 id/空 question/空 label）、handler 正常返回、跳过、custom、handler 缺失、handler 异常、取消、并发串行。
- tests/user_questions/test_tool.py：schema required/additionalProperties、参数错误转 Error: ...、成功 JSON 文本与 fixtures 完全一致、meta、parallel_safe=False。
- tests/tui/test_question_screen.py（若 TUI 可单测）：单选/多选/跳过/自定义/Esc 取消；Textual pilot。
- tests/tools/test_fixtures.py：用 fixtures 的 8 条 ask_user_question 调用参数回放，断言解析与校验通过；结果样本做字符串级对照。
- headless 回归：tests/integration/test_headless.py（Lead 写域）或本模块的 fake runtime 测试，断言无 handler 时是 error result 且 turn 继续。

---

## 3. working_directory

### 3.1 DSH 对应包与关键文件

- **工具**：D:\dshworkspace\_dsh_src\dsh-tool-working-directory\lib\index.js
  - :8 name = "tool-working-directory"；:10 inject = ["tools", "workingDirectory"]。
  - :20-44 schema：只有一个可选 cd（string，描述 Existing directory to enter. Omit to read the current directory.）；hidden output {cwd: string}，required cwd，additionalProperties false；render 返回 value.cwd 纯文本。
  - :40 execute：没有 agent 抛 working_directory requires an agent Session；args.cd === undefined 调 ctx.workingDirectory.ensure(exec.agent, exec.signal)，否则 ctx.workingDirectory.set(exec.agent, args.cd, exec.signal)；返回 {cwd}。
- **服务**：D:\dshworkspace\_dsh_src\dsh-working-directory\lib\index.js
  - :12-18 WorkingDirectoryService，inject ["fs","sessionProjections","systemPrompt"]，Config {defaultDirectory: z.string()}。
  - :25-30 构造：defaultDirectory = config.defaultDirectory ?? process.cwd()；非绝对路径抛 working-directory: defaultDirectory must be absolute。
  - :31-44 注册 session projection：key workingDirectory、stateVersion 1、stateSchema 为“非空绝对路径或 null”；init: header.cwd ?? null；apply 只处理 working-directory/change 的 event.data.cwd；wire view 即 state。
  - :45-51 注册 system prompt context working-directory:current（required true、interpolate false、text = contextText）。
  - :52-69 在 system-prompt/assemble 上 prepend：先 ensure(agent)，再生成文本，确保 context 存在并替换为最新值。
  - :70-73 dispose 时 abort lifecycle 并等待 operations。
  - :80-82 get(session) 无 I/O，返回 projection 值 ?? defaultDirectory。
  - :91-106 ensure：per-session serialize；fs.resolve(current) + fs.stat；目录存在时，如果 projection state 为 null，append working-directory/change {cwd: current} 并返回；不存在则回退 session.header.cwd ?? defaultDirectory，恢复并 commit(recovered=true)。
  - :117-123 set：空路径抛 working-directory: cd must not be empty；directory(path, current) 解析+stat；不是目录抛 working-directory: directory does not exist: <JSON path>；commit 后返回 canonical absolute。
  - :125-127 contextText = Current working directory: <JSON.stringify(directory)>.
  - :138-155 commit：若 previous===directory 直接返回；append working-directory/change {cwd: directory}；构造 user message source={kind:"working-directory"}，文本：
    - 正常：The working directory changed from <JSON previous> to <JSON directory>.
    - 恢复：The working directory <JSON previous> is unavailable. The working directory is now <JSON directory>.
    - agent.inject(notice) 失败只 warn，不回滚已提交状态。
  - :157-166 serialize：每个 session 一条 promise 链（WeakMap pending），lifecycle signal + caller signal 合并；action 前检查 abort 与 fiber active。
- **fixtures 真实记录**：D:\code\deeppyro\fixtures\sessions\session-04a44e0f-...jsonl
  - seq 90 working-directory/change data 精确为 {"cwd":"C:\\Users\\74185\\Desktop\\rushaio-master\\rushaio-master\\src\\rush\\task"}。
  - seq 91 agent/inbox/spliced data：{"target":"next-step","start":0,"inserted":[{"content":[{"type":"text","text":"The working directory changed from \"D:\\\\dshworkspace\" to \"C:\\\\Users\\\\...\\\\task\"."}],"source":{"kind":"working-directory"},"role":"user","id":"..."}]}。
  - 顺序：tool/call(89) → working-directory/change(90) → agent/inbox/spliced(91) → tool/result(92)。工具结果本身是纯路径文本（fixtures 中 C:\Users\...\task，isError=false）。
- **README 补充**：D:\dshworkspace\_dsh_src\dsh-working-directory\README.md 明确 header 仍记 original project；变更串行、经 fs 校验、commit event、再 queue user-context notice；notice 失败仍返回 committed dir；目录与原 project 都不可用时 prompt assembly 拒绝每个 model turn；existing shells/sandbox grants 独立，变更不影响写权限。
- **与 workspace 的区别**：D:\dshworkspace\_dsh_src\dsh-workspace\README.md 的 WorkspaceRegistry 是 host-side 项目列表/会话分组，模型不可见、零 token、无 session events；只与 SessionHeader.cwd 共享字段。working_directory 是模型可见的 per-session 执行目录，不是“切换项目”。

### 3.2 真实语义

1. working_directory() 读取当前 session 的 committed cwd；working_directory({"cd": path}) 设置。无参数时**不**做 I/O，直接返回投影值/默认值。
2. 路径相对当前 cwd 解析；返回 canonical absolute path；结果文本就是该路径（不带 path XML，不带解释）。
3. 变更必须是一个 durable event（working-directory/change），再尝试把 notice 作为 user message 注入；notice 失败不回滚，模型下一轮通过 runtime context 仍能看到新 cwd。
4. header.cwd 不更新；session 的“原始项目”与“当前工作目录”是两个概念。
5. 目录消失时 ensure 会尝试恢复到 header.cwd ?? defaultDirectory；如果恢复目录也无效，抛错，由 prompt assembly 拒绝该 turn（DSH 语义）。
6. 每个 session 的变更串行化；不影响已经在运行的 shell 进程，也不改变 sandbox 写权限。
7. runtime context 行格式：Current working directory: <JSON-quoted absolute path>.；它在 system prompt 之外，runtime context 被禁用时仍要启用（DSH 语义）。
8. cd 为空字符串是错误；目标不存在或不是目录是错误；默认目录必须是绝对路径。

### 3.3 与 DSH 的最小差异

- P1 不实现 ensure 的自动恢复（目录消失→回退原项目）；先返回错误，由用户重新 cd。这是明显的语义缩减，需在 docs/spec/tools.md 记录。
- 不实现 system-prompt context 的 DSH 级插入顺序；复用现有 prompt/runtime_context.py，在 runtime context 里显示 Working directory，并保证每步刷新。
- 不做 per-session WeakMap 的完整 lifecycle abort；用简单的 asyncio.Lock per session + ctx.cancel 检查。
- /cwd slash command 当前是“切换 workspace/project”（tui/app.py:486-514、tui/runtime.py:505-524），与 working_directory 工具不是同一语义。P1 保持 /cwd 不改，新增工具只改变模型侧 cwd；**需 Lead 决策**是否把 /cwd 改成 session cwd（建议保留 /cwd 为 project switch，另加 /cd 或让工具独占）。
- 不新增记录类型；working-directory/change 已在 core.wire.RECORD_TYPES（wire.py:209）。

### 3.4 Python 设计

模块：

    src/deeppyro/working_directory/__init__.py
    src/deeppyro/working_directory/types.py
    src/deeppyro/working_directory/service.py
    src/deeppyro/builtin/working_directory.py
    src/deeppyro/tools/builtin/working_directory.py

类型（types.py）：

    @dataclass(frozen=True, slots=True)
    class WorkingDirectoryState:
        cwd: Path
        recovered: bool = False

服务（service.py）：

    class WorkingDirectoryService(Service):
        provide = "working_directory"
        inject = ["sessions"]

        def __init__(self, ctx: Any, config: Any) -> None:
            """config.defaultDirectory must be absolute; fallback to cwd."""

        def configure(self, *, default_directory: Path, record: RecordFn, inject_notice: NoticeFn) -> None:
            """Install the host callbacks used to append records and queue notices."""

        def get(self, session: Any) -> Path:
            """Return the committed cwd without filesystem I/O."""

        async def ensure(self, session: Any, *, cancel: asyncio.Event | None = None) -> Path:
            """Validate current cwd; P1 returns ToolError if it disappeared."""

        async def set(self, session: Any, path: str | Path, *, cancel: asyncio.Event | None = None) -> Path:
            """Resolve relative to current cwd, validate directory, commit, queue notice."""

        def context_text(self, session: Any) -> str:
            """Current working directory: <json-quoted absolute path>."""

- 状态：dict[str, Path]（session_id → cwd），初始化为 session.header.cwd ?? default_directory；不持久化新状态，启动时由 working-directory/change 记录重放或 header 得到（P1 可只在内存中，session 重开后回到 header.cwd；**需 Lead 决策**是否要 replay）。
- 记录回调签名：Callable[[str, dict[str, Any]], int]，由 AgentSession 注入，内部调用 _append；notice 回调签名：Callable[[str], None]，内部调用 AgentSession.inject(text)。
- set 步骤：serialize lock → 空路径检查 → resolve_path → is_dir → 与 current 相同则直接返回 → record("working-directory/change", {"cwd": str(target)}) → inject_notice(...)（异常只 warning）→ 更新 dict → 返回。
- notice 文案与 DSH 完全一致（用 json.dumps(str(path), ensure_ascii=False) 生成带引号路径）。现有 inject(text)（agent/session.py:305-307）只接收文本，内部 _enqueue -> _new_user_message(text) 把 source 固定为 {"kind":"user"}；需要扩展 inject(text, *, source=None)（或新增 inject_user_context）并在 _enqueue/_new_user_message 中透传 source，否则 notice 的 source.kind 不是 working-directory（**改 agent/session.py，loop-dev 写域，需 Lead 协调**）。
- runtime context：AgentSession._prompt_context()（agent/session.py:1005-1020）当前用 self._cwd；改为优先读 services["working_directory"].get(session)，并在 _tool_context()（:839-850）同样取当前 cwd。这样每个 step 的 runtime context 与工具解析都看到最新值。
- 初始 AgentSession._cwd 保留作为 fallback（没有服务时行为不变）。
- 工具（tools/builtin/working_directory.py）：

    class WorkingDirectoryTool(BaseTool):
        name = "working_directory"
        description = (
            "Read the current working directory, or change it with cd. Relative paths use the "
            "current directory. Existing shells and running processes keep their own directories."
        )
        parameters = {
            "type": "object",
            "properties": {
                "cd": {
                    "type": "string",
                    "description": "Existing directory to enter. Omit to read the current directory.",
                }
            },
        }
        parallel_safe = False

  run 取服务；无 cd → get，有 cd → set；成功 ToolResult(text=str(path), meta={"cwd": str(path)})；服务缺失时回退到 ctx.cwd 只读（或报错，**需 Lead 决策**；建议报错以便暴露接线缺失）。
- 权限：**不**调用 PermissionGate。DSH 明确 cd 不改变 sandbox 写权限；目标目录是否可读由后续 read/write 工具自己 gate。read-only preset 下允许 cd，但后续写仍被拒。
- 审批：不涉及 approval。
- 配置：

    [plugins.working-directory]
    defaultDirectory = "D:/dshworkspace"

  可选；必须是绝对路径，否则服务构造失败（ConfigError 或 ToolError；建议 ConfigError）。
- 持久化：working-directory/change {cwd}；notice 通过 AgentSession.inject 进入 next-step inbox，落盘为 agent/inbox/spliced（与 fixtures 的 seq 91 完全一致，Inbox 是 agent/inbox/spliced 的投影，src/deeppyro/agent/inbox.py:3-5、:217-225、:314），不是直接写 user/message。需要给 inject 增加 source 参数，让 inserted message 的 source.kind=working-directory。

### 3.5 测试策略

- tests/working_directory/test_service.py：get 无 I/O、set 相对路径、绝对路径、空路径、缺失目录、非目录、相同路径不写记录、serialize 并发、notice 异常不回滚、defaultDirectory 相对路径报错。
- tests/working_directory/test_tool.py：schema 只有可选 cd；无参返回 cwd；有参返回 canonical path；服务缺失错误；不作为 parallel_safe。
- tests/agent/test_session_cwd.py：_tool_context().cwd 与 _prompt_context().cwd 随服务变化；runtime context 文本更新；不同 session 隔离。
- tests/tools/test_fixtures.py：回放 fixtures 的 2 条 working_directory 调用参数，断言 schema 接受、结果形态纯路径。
- 记录回归：working-directory/change 形状与 fixtures 精确一致；notice 文本与 fixtures 一致（含 JSON 引号）。

---

## 4. 图片附件与 image offload

### 4.1 DSH 对应包与关键文件

- **附件存储 seam**：D:\dshworkspace\_dsh_src\dsh-attachment
  - README.md（138 行）核心语义：PNG/JPEG/WebP/GIF 在消息接收前 normalize 为 provider-independent 8-bit sRGB/sRGBA raster 并持久化；其他文件 verbatim 保存、无类型/大小限制；模型只收到 deterministic handle line（文件名、字节数、digest prefix、只读 path），不收到 bytes；durable session events 不含浏览器路径/provider URL/local storage path/base64；attachments 永不自动删除；未发送草稿不保存。
  - lib/index.js:5-26 错误码：TOO_MANY_IMAGES、IMAGES_TOO_LARGE、UNSUPPORTED_IMAGE_TYPE、INVALID_IMAGE_BASE64、INVALID_IMAGE、IMAGE_TYPE_MISMATCH、IMAGE_TOO_LARGE、IMAGE_TOO_MANY_PIXELS、IMAGE_DIMENSION_TOO_LARGE、INVALID_FILE_BASE64、INVALID_ATTACHMENT_REF、ATTACHMENT_CORRUPT、ATTACHMENT_WRITE_FAILED、ATTACHMENT_NOT_FOUND、ATTACHMENT_READ_FAILED、ATTACHMENT_PROJECTION_UNSUPPORTED、ATTACHMENT_FILES_UNSUPPORTED。
  - lib/index.js:39-52 AttachmentError（code + message）。
  - lib/index.js:150-178 requestImageDimensions：按 sqrt(maxPixels/(w*h)) 缩放，向内取整，不放大。
  - lib/index.js:188-200 longEdgeDimensions：长边精确、短边 round。
  - lib/index.js:218-357 AttachmentStore（provide attachments）：validateImageBatch :230-234、saveImages :240-246、admitPromptContent :257-281、admitEncodedFile :288-289、imageHostPath :305、saveFile :314-316（默认 reject）、readFileStream :335-338、readImageRequest :353-356（默认 reject）。
  - 注意：attachment 只定义 seam；filesystem/blob provider 在其他包，本机未提供。Python 版必须自建 store。
- **消息内容形状**：D:\dshworkspace\_dsh_src\dsh-commands\lib\typert.host.js
  - :288-289 AttachmentId = Branded<'AttachmentId'>。
  - :468-477 ImageAttachmentRef {attachmentId, mediaType, bytes, width, height, name?, originalDimensions?}；ImageBlock {type:'image', attachment: ImageAttachmentRef, offloaded?: true}；ImageMediaType = 'image/png' | 'image/jpeg' | 'image/webp' | 'image/gif'。
  - :480-482 ImageOffloadTarget {seq: SessionSeq; imageIndexes: number[]}。
  - :709 SessionEventMap['image/offload'] = {targets: ImageOffloadTarget[]}；同处 working-directory/change、approval/* 的形状。
  - :412-417 FileAttachmentRef {attachmentId, name, bytes}；FileBlock {type:'file', attachment: FileAttachmentRef}。
- **offload 文本与预算算法**：D:\dshworkspace\_dsh_src\dsh-llm\lib\index.js
  - :581-585 offloadedImageText(ref, access)：[image omitted to fit request image limits; <imageIdentity>. No local normalized image path is available; ask the user to attach it again if needed.]；有 access 时追加 Normalized copy (read-only; may be resized or re-encoded): <readonlyPath> (<width>x<height>px, <mediaType>). Source dimensions, format, and byte size may differ. Copy to a writable path ending in <ext> before editing.
  - :571-574 requestImageHandleText：Image <identity>; request preview <w>x<h>px. + access 说明。
  - :558-559 textOnlyImageText：[image omitted because this model accepts text only; attachment sha256:<attachmentId slice(7,15)>]。
  - :614-618 fileHandleText：File <quoted name> (<bytes> bytes, sha256:<digest>)；无 path 时说明 execution environment 不可读；有 path 时说明 read-only copy 路径与编辑前复制。
  - :647-649 base64Length(bytes) = ceil(bytes/3)*4。
  - :692-709 offloadedImagePrefixCount(lengths, budget)：excessCount = len - maxImages，excessBytes = total - maxBytes；removeCount = ceil(excessCount/countQuantum)*countQuantum；removeBytes = ceil(excessBytes/byteQuantum)*byteQuantum；从最旧图片开始累计，直到同时满足 count 与 bytes（byteQuantum==1 时用 >=，否则 >）；返回需要 offload 的前缀数量。
  - :720-728 requiredImageOffload(messages, budget, versionBytes)：跳过已 offloaded；按 route representation 计算 base64Length 或原 bytes；返回还差多少最旧图片需要 offload。
- **provider 预算常量**：D:\dshworkspace\_dsh_src\dsh-llm-deepseek\lib\index.js
  - :44 DEFAULT_IMAGE_OFFLOAD_BYTE_QUANTUM = 64 * 1024 * 1024（67108864）。
  - :46 DEFAULT_INLINE_IMAGE_OFFLOAD_BYTE_QUANTUM = 10 * 1024 * 1024（10485760）。
  - :48 DEFAULT_IMAGE_OFFLOAD_COUNT_QUANTUM = 20。
  - :418-423 maxImagesPerRequest=600、maxRequestFilesBytes=134217728、maxInlineRequestImageBytes=20971520。
  - :424-432 config 默认与约束：imageOffloadByteQuantum=67108864、inlineImageOffloadByteQuantum=10485760、imageOffloadCountQuantum=20；必须分别 <= maxRequestFilesBytes、<= maxInlineRequestImageBytes、<= maxImagesPerRequest。
  - :433-440 filesApiTimeoutMs=60000、fileExpiresAfterSeconds=604800、fileRefreshMarginSeconds=3600。
  - :1461-1462 图片超额时抛 LlmError：DeepSeek Messages <representation> request images exceed the route budget; <n> more oldest occurrence(s) must be offloaded.，code IMAGE_OFFLOAD_REQUIRED；由 compaction image-offload 记录 selection 后重试（dsh-llm lib/index.js:231-233）。
- **session 投影**：D:\dshworkspace\_dsh_src\dsh-session\lib\known-event-types.js:85-88 MESSAGE_PROJECTION_EVENT_TYPES = new Set(['image/offload'])；说明 image/offload 必须由显式 pure interpreter 产生模型可见效果。
- **fixtures**：可参考 fixtures/records/by-type/request_header.jsonl 的 image/offload 记录（若存在）与 messages 中 image part；但 30 个 session 中没有真实图片样本（文件大部分是文本），因此 offload 行为只能由 DSH 源码常量与算法验证。
- **仓库现状**：
  - src/deeppyro/core/wire.py:42-44 已有 ImagePart(type="image", attachment: str | dict)。
  - src/deeppyro/core/wire.py:214 已声明 MESSAGE_PROJECTION_TYPES=("image/offload",)；RECORD_TYPES 已含 image/offload（:170）。
  - src/deeppyro/session/derive.py:70-73 目前对 PROJECTION_EVENTS 直接 SKIP，注释 v1 has no projection interpreter (image/offload)。
  - src/deeppyro/llm/messages.py:92-112 image_part_url 只认 data:/http(s) URL 或 dict{url|data|base64,mimeType}；裸 attachment id 返回 None 被跳过；:143-157 只在 user message 有 image 时转 image_url。
  - TUI 没有图片附件输入路径；P1 的附件来源只能是 SDK/测试/未来 IM。

### 4.2 真实语义

1. **附件与消息分离**：session/durable event 只存 ImageAttachmentRef（id、mediaType、bytes、width、height、name?、originalDimensions?），不存 base64、不存浏览器路径；原始 bytes 由 store 保存。
2. **normalize**：图片在接收时解码、按 EXIF 方向校正、转 8-bit sRGB/sRGBA、按 target 尺寸重编码；模型请求拿到的是 deterministic request version，identity 含 attachment id、transform version、target dimensions、byte target、encoder settings。
3. **admission**：仅 PNG/JPEG/WebP/GIF；每消息图片数量与总 bytes 有上限；超限报 TOO_MANY_IMAGES/IMAGES_TOO_LARGE；不支持类型/坏 base64/像素/dimension 超限都有稳定错误码。
4. **offload 是“最旧优先”的预算动作**：当请求的图片数量或 bytes 超过 provider budget，按 offloadedImagePrefixCount 把最旧的前缀图片替换为文本占位；image/offload 记录 {targets:[{seq, imageIndexes:[...]}]}，其中 seq 是消息的 session seq，imageIndexes 是该消息 content 中图片的下标。
5. **投影**：image/offload 不是 surface event，而是 pure projection；derived messages 中对应 ImagePart 被替换为 offloadedImageText。这样同一份 durable log 可以在不同 provider/预算下重放出不同请求。
6. **provider 错误**：图片仍超预算时 provider 抛 IMAGE_OFFLOAD_REQUIRED，附带“还需 offload 多少张最旧图片”；compaction 侧记录 selection 后重试。P1 可以先在请求组装前主动 offload，不做 error-retry 路径。
7. **文本模型**：textOnlyImageText 用于只接受文本的模型，不需要 offload 记录。
8. **文件附件**：非图片文件 verbatim 保存，模型收到 fileHandleText（文件名、bytes、sha256、只读路径），不是 bytes；P1 可只做图片，文件后续。

### 4.3 与 DSH 的最小差异

- P1 只做 **图片**，不做通用 FileBlock/FileAttachmentRef。
- P1 的附件来源只支持显式 API（AttachmentStore.save_image 或 ImagePart 中已带 ref），不做 TUI 上传、剪贴板、IM。
- 不做 files API 上传与过期刷新（filesApiTimeoutMs 等）；只走 inline base64 或 data URL。
- 不做自动删除/GC；附件目录只增不减（与 DSH 一致）。
- 不做 provider 侧的 IMAGE_OFFLOAD_REQUIRED 重试；在请求组装时一次算够。
- offload 的文本占位必须与 DSH 完全一致；如果 Python 版没有 read-only path，使用无 path 版本。
- 需要改 session/derive.py（session-dev 写域）与 llm/messages.py（llm-dev 写域），**必须 Lead 协调写域**；本任务只写文档。

### 4.4 Python 设计

分两阶段，避免一次改动过大。

#### 阶段 1：AttachmentStore + 引用解析（可独立交付）

模块：

    src/deeppyro/attachments/__init__.py
    src/deeppyro/attachments/types.py
    src/deeppyro/attachments/store.py
    src/deeppyro/builtin/attachments.py

类型（types.py）：

    ImageMediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]

    @dataclass(frozen=True, slots=True)
    class ImageAttachmentRef:
        attachmentId: str
        mediaType: ImageMediaType
        bytes: int
        width: int
        height: int
        name: str | None = None
        originalDimensions: dict[str, int] | None = None

    @dataclass(frozen=True, slots=True)
    class FileAttachmentRef:
        attachmentId: str
        name: str
        bytes: int

服务（store.py）：

    class AttachmentStore(Service):
        provide = "attachments"
        inject = []

        def __init__(self, ctx: Any, config: Any) -> None:
            """config.directory fallback: user_data_dir("deeppyro") / "attachments"."""

        async def save_image(
            self,
            data: bytes,
            *,
            name: str | None = None,
            media_type: str | None = None,
        ) -> ImageAttachmentRef:
            """Decode/validate/normalize/persist; return ref. Raises AttachmentError."""

        async def save_file(self, data: bytes, *, name: str) -> FileAttachmentRef:
            """Verbatim bytes; P1 可选。"""

        def image_host_path(self, ref: ImageAttachmentRef) -> Path:
            """Return the normalized read-only path."""

        def read_image_bytes(self, ref: ImageAttachmentRef) -> bytes:
            """Verify digest/format/bytes/dimensions; raise ATTACHMENT_CORRUPT / NOT_FOUND."""

        def lookup(self, attachment_id: str) -> ImageAttachmentRef | None:
            """Resolve a bare attachment id from the manifest."""

- 实现：Pillow（已装）Image.open → ImageOps.exif_transpose → convert("RGB")/RGBA → 保存为 PNG（统一格式，避免原始格式差异）；读取时校验 format 在白名单、尺寸/像素上限、digest（manifest 里存 sha256）。
- 目录：<data_dir>/attachments/<attachmentId>.<ext> + manifest.jsonl（或每附件一个 json sidecar），避免 session event 依赖具体路径。
- ID：att-<uuid4().hex[:16]>；ImageAttachmentRef.attachmentId 为裸 id。
- 安全：Pillow 解压炸弹防护 Image.MAX_IMAGE_PIXELS，并显式检查 width*height、单边长度；异常转稳定错误码（INVALID_IMAGE、IMAGE_TOO_MANY_PIXELS、IMAGE_DIMENSION_TOO_LARGE、UNSUPPORTED_IMAGE_TYPE、TOO_MANY_IMAGES、IMAGES_TOO_LARGE）。
- 配置：

    [attachments]
    directory = ""              # 空表示 user_data_dir/deeppyro/attachments
    maxImagesPerMessage = 20
    maxImageBytes = 20971520
    maxPixels = 40000000
    maxDimension = 16384

- 权限/审批：保存附件不涉及 sandbox；读 attachment host path 是只读，不触发写 gate。若以后允许模型写附件目录，再走 gate。

#### 阶段 2：请求预算 + offload 记录 + 投影

模块：

    src/deeppyro/llm/image_budget.py

public API：

    @dataclass(frozen=True, slots=True)
    class ImageBudget:
        max_images: int = 600
        max_bytes: int = 134217728
        max_inline_bytes: int = 20971520
        offload_byte_quantum: int = 67108864
        inline_offload_byte_quantum: int = 10485760
        offload_count_quantum: int = 20

    def base64_length(n: int) -> int: ...
    def offloaded_image_prefix_count(lengths: Sequence[int], budget: ImageBudget) -> int: ...
    def required_image_offload(
        messages: Sequence[Message],
        budget: ImageBudget,
        *,
        version_bytes: Callable[[ImagePart], int] | None = None,
    ) -> int: ...

- 放置位置：llm/image_budget.py 只依赖 core.wire，不 import attachments；Message 的 ImagePart 需要能给出“请求 representation 的 bytes”，由调用方传 version_bytes。
- 请求组装：在 src/deeppyro/llm/ 的 provider 请求构建处（llm-dev 写域）调用 required_image_offload；如果 >0，说明需要新的 image/offload 记录，而不是静默丢弃图片。
- 记录：ctx.log("image/offload", {"targets":[{"seq": <message seq>, "imageIndexes":[...]}]})；需要在 AgentSession 层做，因为只有它有 session seq 与 durable append。image/offload 已在 RECORD_TYPES，无需改 core。
- 投影：修改 src/deeppyro/session/derive.py，新增 pure interpreter：
  - project_image_offloads(records: Sequence[LogRecord]) -> dict[int, set[int]]：收集所有 image/offload，按 target.seq 合并 imageIndexes（后写覆盖/并集；DSH 是 selection，重复应幂等）。
  - derive_messages：先 fold_surface，对每个 surface 节点的原始 seq 查 offload 集合；event_message 后，将对应 ImagePart 替换为 TextPart(text=offloaded_image_text(ref))。文本沿用 DSH 的 no-path 版本；如果 store 提供只读路径，用带 access 的版本。
  - 若记录 target 的 seq 不在 surface（被 replace/compaction 掉），直接忽略，不报错（与 projection 容错一致）。
- 触发时机：**需 Lead 决策**。建议在每次构建请求前计算；如果超预算，append 一条 image/offload 记录并重建请求。不要在 provider 返回 400 后才做（P1 简化）。
- 配置：上述 ImageBudget 放到 provider 配置（ProviderConfig.extra）或 [llm]；P1 建议 [llm.imageBudget]，用 Config.extra 承载，不改 config.py。
- 权限/审批：offload 是纯展示/预算变换，不涉及 sandbox；不需要 approval。

ImagePart 与 llm/messages.py 接线：
- 现有 ImagePart.attachment 是 str | dict，可直接承载完整 ref dict（不修改 core 类型）：

    {"attachmentId": "...", "mediaType": "image/png", "bytes": 123, "width": 800, "height": 600}

- image_part_url(part) 增加可选 resolver：image_part_url(part, *, attachments: AttachmentResolver | None = None)；裸字符串 id 且 resolver 存在时 → attachments.image_host_path 读 bytes → data URL。**改 llm/messages.py 是 llm-dev 写域，需 Lead 协调**。
- 如果不想改函数签名，也可以在 AgentSession 组装 messages 时把 ref 展开为 data URL；但这会把 base64 写进 session/内存，破坏“durable 不含 base64”的原则，不推荐。

### 4.5 测试策略

- tests/attachments/test_store.py：Pillow 生成 PNG/JPEG/WebP/GIF；EXIF 方向；RGBA；超大像素/尺寸；坏 bytes；不支持类型；ref round-trip；digest/bytes 校验；目录不存在时创建；manifest 损坏。
- tests/attachments/test_types.py：ref 序列化/反序列化、字段可选性、media type 枚举。
- tests/llm/test_image_budget.py：base64_length；offloaded_image_prefix_count 的 count 超限、bytes 超限、quantum>1 的 > 边界、quantum==1 的 >= 边界；required_image_offload 跳过已 offloaded。
- tests/session/test_image_projection.py：单条/多条 image/offload；同一 seq 多次记录；target 不在 surface；ImagePart 替换为 TextPart；不影响其他 part；文本与 DSH 常量一致。
- tests/llm/test_messages.py：ImagePart ref + resolver → data URL；无 resolver 的裸 id 跳过（保持现有行为）；文本模型路径。
- fixtures：fixtures 里没有真实图片，测试必须用 Pillow 生成；不要声称 fixtures 覆盖。

### 4.6 风险

- **最大风险**：投影解释器位于 session/derive.py（session-dev 写域），而请求预算位于 llm（llm-dev 写域），本特性跨三个 workstream；如果写域不拆清，容易冲突。
- Pillow 解码不可信图片有内存/CPU 风险；必须设 MAX_IMAGE_PIXELS 与单边/像素上限，并在 decode 前检查 header 尺寸。
- image/offload 的 seq 必须是 session 记录 seq；在 agent/session.py 中 append 后才能知道，不能在 llm 层凭空造 seq。
- 如果 provider 只支持 inline 且没有 files API，预算常量需要按我们的模型能力重新定；不能直接照搬 DeepSeek 的 600 张/128MB（那是 DeepSeek 路由）。
- 不做 base64 持久化是硬约束；任何“把图片 bytes 塞进 session”的实现都应拒绝。

---

## 5. 跨特性公共改动与接线清单

| 改动点 | 文件 | 归属 | 说明 |
|---|---|---|---|
| 新服务注册行 | bundles/core/bundle.toml | Lead | working_directory / user_questions / attachments（若启用）各加一行；顺序在 tools 之后、agents 之前 |
| 服务注入 ToolContext | src/deeppyro/tui/runtime.py:294-310、src/deeppyro/headless.py:370-391 | tui-dev / Lead | 加入 user_questions、working_directory、attachments；PTC 若做再加 ptc |
| cwd 动态解析 | src/deeppyro/agent/session.py:839-850、:1005-1020 | loop-dev | 从 services 读 working_directory；保留 _cwd fallback |
| user context notice | src/deeppyro/agent/session.py:305-307 | loop-dev | inject 需要支持 source；或新增 inject_user_context |
| image/offload 投影 | src/deeppyro/session/derive.py:66-73、:213-221 | session-dev | 实现 pure interpreter；需要 core 已声明 MESSAGE_PROJECTION_TYPES |
| image 引用解析 | src/deeppyro/llm/messages.py:92-112 | llm-dev | 接受 attachments resolver / ref dict |
| 内置工具注册 | src/deeppyro/tools/builtin/__init__.py | tools-dev | working_directory、ask_user_question 加入 builtin_tools 列表；顺序会影响 schema 前缀稳定性 |
| 记录类型 | src/deeppyro/core/wire.py | Lead | 已存在 working-directory/change、image/offload、tool/ptc-dispatch*，**无需新增** |
| 配置 | src/deeppyro/config.py | Lead | P1 建议用 Config.extra 承载插件配置，避免改冻结 config；若强类型再改 |

**建议的 service 命名**（Python 风格 snake_case，括号内为 DSH 对应名）：

- working_directory（DSH workingDirectory）
- user_questions（DSH userQuestions）
- attachments（DSH attachments）
- ptc（DSH ptcRuntime，仅方案 B）

如果 Lead 希望与 DSH 名字逐字一致，也可用 camelCase；但现有 Python 服务全部小写/snake，建议保持 Python 风格，并在文档注明映射。

---

## 6. 测试策略总览

### 6.1 单测文件清单（建议新增）

    tests/working_directory/__init__.py
    tests/working_directory/test_service.py
    tests/working_directory/test_tool.py
    tests/working_directory/test_agent_cwd.py
    tests/user_questions/__init__.py
    tests/user_questions/test_service.py
    tests/user_questions/test_tool.py
    tests/user_questions/test_tui_handler.py
    tests/attachments/__init__.py
    tests/attachments/test_store.py
    tests/attachments/test_types.py
    tests/llm/test_image_budget.py
    tests/session/test_image_projection.py
    tests/ptc/__init__.py
    tests/ptc/test_runtime.py
    tests/ptc/test_bindings.py
    tests/ptc/test_tool.py
    tests/ptc/test_dispatch_records.py

### 6.2 fixture 需求

- ask_user_question：已有 8 条真实调用，足够做参数/schema/结果回归；不需要新 fixture。
- working_directory：已有 2 条调用 + 1 条 change + 1 条 inbox/spliced 记录，足够做语义回归；不需要新 fixture。
- image offload：fixtures 没有图片样本；需要 Pillow 生成的临时图片（PNG/JPEG/WebP/GIF、EXIF、超大尺寸），并在测试中自建小 session log；不需要修改 fixtures。
- run_code：fixtures 没有 run_code；只能自造 schema/结果测试，不能声称对齐 DSH。
- 所有新测试使用 tmp_path，不写仓库 fixtures；网络测试继续用 respx mock，不打外网。

### 6.3 回归命令

    uv run pytest -o addopts= -q tests/working_directory tests/user_questions tests/attachments tests/session/test_image_projection.py
    uv run pytest -o addopts= -q tests/ptc
    uv run ruff check src/deeppyro/working_directory src/deeppyro/user_questions src/deeppyro/attachments src/deeppyro/ptc tests/working_directory tests/user_questions tests/attachments tests/ptc

注意 pyproject.toml 的 addopts = "-q"；要统计数量时用 -o addopts=""，否则 -qq 会吞掉 summary。

---

## 7. 实施拆分（shared task 建议）

依赖顺序：T1 与 T2 可并行；T3 依赖 T1（需要 cwd 动态解析的 session 接线）；T4 依赖 T1/T2 的 service 模式；T5 可独立 spike。

| 任务 | 标题 | 写域 | 依赖 | 预估 |
|---|---|---|---|---|
| T1 | working_directory 服务 + 工具 + runtime context/AgentSession 接线 | src/deeppyro/working_directory/**、src/deeppyro/tools/builtin/working_directory.py、src/deeppyro/builtin/working_directory.py、tests/working_directory/**、bundles/core/bundle.toml（Lead 批准） | 无 | 1.5-2d |
| T2 | ask_user_question 服务 + 工具 + TUI 问题弹窗 + headless 错误 | src/deeppyro/user_questions/**、src/deeppyro/tools/builtin/ask_user.py、src/deeppyro/builtin/user_questions.py、src/deeppyro/tui/**（Lead 协调）、tests/user_questions/** | 无 | 2-2.5d |
| T3 | image 附件存储 + ImagePart resolver + 预算算法 + image/offload 投影 | src/deeppyro/attachments/**、src/deeppyro/llm/image_budget.py、src/deeppyro/session/derive.py（session-dev 协调）、src/deeppyro/llm/messages.py（llm-dev 协调）、tests/attachments/**、tests/llm/test_image_budget.py、tests/session/test_image_projection.py | T1（cwd/服务模式可复用；实际只依赖 core） | 3-4d，跨 3 个写域 |
| T4 | image offload 请求接线 + provider 预算配置 + retry 路径 | src/deeppyro/llm/**（llm-dev）、src/deeppyro/agent/session.py（loop-dev）、src/deeppyro/config.py（Lead） | T3 | 2-3d |
| T5 | run_code/PTC spike（只做决策材料，不默认合入） | docs/plan/** 或 src/deeppyro/ptc/** 原型 | 无 | 0.5-1d spike；正式实现 4-6d |
| T6 | 集成验收：TUI 问问题、cd 后 read/write 路径、图片超预算请求 | tests/integration/**（Lead） | T1/T2/T3 | 1-2d |

建议先做 T1+T2（低风险、可并行、能立刻提升模型能力），T3 在 Lead 确认写域后启动；T5 只做 spike，除非 Lead 明确要 P1 上线 run_code。

---

## 8. Lead 必读结论（建议 3-8 条）

1. **run_code 建议 P2，不纳入 P1 实现。** DSH 的 run_code 是 PTC transport，不是普通工具；本机没有 dsh-tool-executor-ptc 源码，fixtures 也没有 run_code，schema/行为无法逐行对齐。Python 版若要真执行，必须自建 subprocess + binding RPC，且当前 PermissionGate 不是 OS 沙箱；建议只保留“保留名 + transport 接缝”，P1 不注册该工具。

2. **working_directory 是最确定的 P1 特性。** 记录类型、runtime context、工具 schema 都已在仓库/fixtures 中；唯一需要 Lead 协调的是 AgentSession._cwd 改为从服务动态读取，以及 inject 支持 source.kind="working-directory"。建议 P1 不做目录消失自动恢复，先报错。

3. **ask_user_question 的 P1 范围应限定为 blocking legacy。** fixtures 的 8 条真实调用全部是无 timeout 的阻塞模式；结果必须是单个 JSON 字符串。TUI 需要一个独立的问题弹窗；headless 无 answerer 时返回错误结果即可。timed/pending 是 P2。

4. **图片 offload 的关键不在图片解码，而在 session 投影。** image/offload 已在 core 声明为 MESSAGE_PROJECTION_TYPES，但 session/derive.py:70-73 目前直接 SKIP；不实现 pure interpreter，offload 记录就没有模型可见效果。该文件属于 session-dev 写域，必须 Lead 明确协调，否则不要开工 T3。

5. **图片引用解析会触碰 llm/messages.py。** 当前 image_part_url 会跳过裸 attachment id（llm/messages.py:92-103）。要么增加 resolver 参数（改 llm-dev 写域），要么在 AgentSession 组装时展开为 data URL（会把 base64 带进内存，违背 durable 不含 base64 的原则）。推荐前者，且需要 Lead 拍板。

6. **服务命名建议 snake_case，并在文档注明 DSH 映射。** working_directory / user_questions / attachments / ptc；DSH 对应 workingDirectory / userQuestions / attachments / ptcRuntime。现有 Python 服务全部小写/snake，不建议为 4 个新服务引入 camelCase 特例。

7. **配置优先走 Config.extra，不要改冻结的 config.py。** 4 个特性的配置都可以放在 [plugins.*] 或 [llm.imageBudget]，通过 pydantic extra="allow" 读取；只有 Lead 同意强类型时才改 config.py。这样能避免与当前并行 workstream 冲突。

8. **写域冲突是本轮最大执行风险。** T1/T2 可独立；T3/T4 横跨 attachments（新）、session/derive.py（session-dev）、llm/messages.py（llm-dev）、config.py（Lead），建议拆成 3 个 shared task 并明确“谁改哪个文件”，不要由一个人跨域修改。

---

## 附：本次调研的临时脚本（不提交）

- D:\dshworkspace\_scratch_tools\extract_p1_schemas.py：从 fixtures/records/by-type/request_header.jsonl 抽 ask_user_question / working_directory 等 7 个工具 schema。
- D:\dshworkspace\_scratch_tools\scan_run_code.py：扫描 30 个 session 的 request_header，确认无 run_code。
- D:\dshworkspace\_scratch_tools\extract_typert.py：从 dsh-commands/lib/typert.host.js 抽 Ptc/Image/WorkingDirectory 类型声明。


