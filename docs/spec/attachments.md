# Attachments 规格

本文记录 DeepPyro P1 图片附件的阶段 1：附件引用类型、AttachmentStore（校验 / 归一化 / 持久化 / manifest）、以及 attachments 服务插件。实现参考 DSH 的 dsh-attachment 包与 docs/plan/p1-tools-runtime.md §4.4，但按 P1 范围裁剪：只做图片，不做通用 FileBlock、files API、自动删除与 image offload。阶段 2（image offload 预算、session 投影、llm resolver）不在本层，见 §10。

## 1. 范围

- 本层负责：PNG / JPEG / WebP / GIF 图片的校验与错误码、统一归一化为 PNG、持久化到附件目录、manifest.jsonl 记录、`ImageAttachmentRef` / `FileAttachmentRef` 类型与 DSH wire 序列化、`attachments` 服务插件、[attachments] 配置。
- 本层不负责：TUI 上传 / 剪贴板 / IM 附件输入；通用文件附件与 files API；附件自动删除或 GC；image offload 请求预算、`image/offload` 记录与 `session/derive.py` 投影；`bundle.toml` 行注册；`INTERFACES.md`。
- 阶段 2 由其他 owner 完成：`src/deeppyro/llm/image_budget.py`、`src/deeppyro/session/derive.py` 的投影解释器、`src/deeppyro/llm/messages.py` 的 resolver 接线。本层只保证这些模块需要的引用数据与读接口稳定。

## 2. 文件布局

- @src/deeppyro/attachments/__init__.py：公开 API 重导出。
- @src/deeppyro/attachments/types.py：`ImageMediaType`、`ImageAttachmentRef`、`FileAttachmentRef`、附件 ID 前缀，以及 DSH wire（camelCase）的 `to_dict()` / `from_dict()`。
- @src/deeppyro/attachments/store.py：`AttachmentStore`、`AttachmentConfig`、`AttachmentError`、稳定错误码、默认目录与常量。
- @src/deeppyro/builtin/attachments.py：`AttachmentsPlugin`，把 store 注册为 `attachments` 服务。
- 测试位于 @tests/attachments/：`test_types.py`、`test_store.py`、`test_plugin.py`。

## 3. 数据结构

### 3.1 ImageAttachmentRef

```python
@dataclass(frozen=True, slots=True)
class ImageAttachmentRef:
    attachmentId: str
    mediaType: ImageMediaType
    bytes: int
    width: int
    height: int
    name: str | None = None
    originalDimensions: dict[str, int] | None = None
```

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `attachmentId` | str | 裸附件 ID，形如 `att-<uuid4().hex[:16]>`（`att-` + 16 位小写 hex） |
| `mediaType` | ImageMediaType | 存储版本总为 `image/png`（归一化后） |
| `bytes` | int | 存储 PNG 的字节数，>= 0 |
| `width` / `height` | int | 存储 PNG 的像素尺寸，> 0 |
| `name` | str 或 None | 原始文件名（仅展示用，不参与路径拼接） |
| `originalDimensions` | dict 或 None | 归一化前的 header 尺寸；仅当与存储尺寸不同时存在（如 EXIF 旋转） |

- 字段名是 DSH wire 的 camelCase；`to_dict()` 输出同样 camelCase，并省略值为 None 的可选字段；`from_dict()` 校验必填字段、media type 白名单、整数语义（拒绝 bool）与 `originalDimensions` 形状，字段类型不符抛 `TypeError`，取值非法（空 ID、未知 media type、负字节数、非正尺寸）抛 `ValueError`。
- 构造时 `__post_init__` 同样校验不变量：ID 非空、media type 在枚举内、bytes >= 0、宽高 > 0。
- durable session event 里只允许出现 ref（或其 dict 形式），绝不允许 base64 或本地路径。

### 3.2 FileAttachmentRef

```python
@dataclass(frozen=True, slots=True)
class FileAttachmentRef:
    attachmentId: str
    name: str
    bytes: int
```

P1 只定义类型与 wire 序列化，不提供 `save_file` 实现；通用文件附件留待后续阶段。

### 3.3 ImageMediaType

`ImageMediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]`，顺序固定为 `IMAGE_MEDIA_TYPES` 常量。仅这四种媒体类型可作为上传来源；存储后一律为 `image/png`。

## 4. 存储布局与写路径

- 目录：默认 `deeppyro_home() / "attachments"`（`~/.dpro/attachments`，遵循 `DEEPPYRO_HOME`）；可由 `[attachments].directory` 覆盖；构造参数 `directory` 优先级最高。相对路径按当前工作目录 resolve。
- 文件：

```text
<directory>/
    manifest.jsonl
    att-0123456789abcdef.png
```

- 每个附件一个 `<attachmentId>.png`；`manifest.jsonl` 是 append-only JSON Lines，一行一条记录（键：`id`、`mediaType`、`bytes`、`width`、`height`、`name`、`digest`、`created_at`）；`digest` 是存储 PNG 的 sha256 hex；`created_at` 是 UTC ISO-8601 毫秒（`...Z`）。
- 写路径：校验 → 归一化 → 生成 ID 与 digest → 目录 `mkdir(parents=True, exist_ok=True)` → 临时文件 + `fsync` + `os.replace` 原子写 PNG → append manifest 行并 `flush` + `fsync`。崩溃最多留下没有 manifest 记录的孤儿 PNG，不会产生指向坏文件的 ref。
- 归一化：`Pillow Image.open` → 显式 header 尺寸检查 → `image.load()` → `ImageOps.exif_transpose` → 有 alpha（`RGBA` / `LA` / `PA` / 带 transparency 的 `P`）转 `RGBA`，否则转 `RGB` → 重编码为 PNG。所有 ref 的 `mediaType` 都是 `image/png`。
- `originalDimensions`：取归一化前 header 的尺寸（EXIF 未应用），仅当与存储尺寸不同才记录。

## 5. 校验、限额与错误码

### 5.1 检查顺序

1. 类型：只接受 `bytes/bytearray/memoryview`；空数据拒绝（`INVALID_IMAGE`）。
2. 原始 payload 字节上限 `maxImageBytes`（`IMAGES_TOO_LARGE`）。
3. 调用方声明的 `media_type`：`image/jpg` 归一为 `image/jpeg`；不在白名单拒绝（`UNSUPPORTED_IMAGE_TYPE`）。
4. Pillow 读 header 识别真实格式；只认 PNG / JPEG / WebP / GIF（其它 `UNSUPPORTED_IMAGE_TYPE`）；坏字节 `INVALID_IMAGE`；与声明的 media type 不一致 `INVALID_IMAGE`。
5. 单边长度 <= `maxDimension`（`IMAGE_DIMENSION_TOO_LARGE`）；宽 * 高 <= `maxPixels`（`IMAGE_TOO_MANY_PIXELS`）。这两步在 decode 前执行，避免解压炸弹。
6. 解码并归一化；归一化后 PNG 字节仍不得超过 `maxImageBytes`（`IMAGES_TOO_LARGE`）。

Pillow 的 `Image.MAX_IMAGE_PIXELS` 解压炸弹保护保留：超过 Pillow 阈值时其 `DecompressionBombError` 也映射为 `IMAGE_TOO_MANY_PIXELS`。

### 5.2 错误码

`AttachmentError` 携带稳定 `code` 与人类可读 `message`，`str(err)` 形如 `CODE: message`。调用方必须按 code 分支，不得解析 message。

| code | 触发 |
| ---- | ---- |
| `INVALID_IMAGE` | 空数据、非字节数据、无法识别的图片、声明类型与真实格式不一致、宽高非法 |
| `UNSUPPORTED_IMAGE_TYPE` | 格式或声明的 media type 不在 PNG / JPEG / WebP / GIF 白名单 |
| `IMAGE_TOO_MANY_PIXELS` | 宽 * 高超过 `maxPixels`，或触发 Pillow 解压炸弹阈值 |
| `IMAGE_DIMENSION_TOO_LARGE` | 任一边超过 `maxDimension` |
| `TOO_MANY_IMAGES` | 单消息图片数量超过 `maxImagesPerMessage`（`validate_image_batch`） |
| `IMAGES_TOO_LARGE` | 原始或归一化 payload、或单消息图片总字节超过 `maxImageBytes` |
| `ATTACHMENT_NOT_FOUND` | 附件文件不存在，或 ID 非法（不能拼出路径） |
| `ATTACHMENT_CORRUPT` | 读取失败、字节数与 ref 不符、manifest 无记录、digest 不符、存储文件不是可读 PNG 或尺寸不符 |

### 5.3 配置

```toml
[attachments]
directory = ""              # 空表示 deeppyro_home()/attachments
maxImagesPerMessage = 20
maxImageBytes = 20971520    # 20 MiB
maxPixels = 40000000
maxDimension = 16384
```

- 对应 `AttachmentConfig`（pydantic），camelCase 别名与 snake_case 字段名都接受；未知键与越界值抛 `ConfigError`（`invalid [attachments] configuration: ...`）。
- 默认值常量：`DEFAULT_MAX_IMAGES_PER_MESSAGE=20`、`DEFAULT_MAX_IMAGE_BYTES=20*1024*1024`、`DEFAULT_MAX_PIXELS=40_000_000`、`DEFAULT_MAX_DIMENSION=16_384`。

## 6. 公开 API

`AttachmentStore` 是普通类（不是 cordis Service），可独立构造与测试；插件层负责把它 provide 成服务。

```python
class AttachmentStore:
    provide = "attachments"
    inject = []

    def __init__(
        self,
        config: AttachmentConfig | Mapping[str, Any] | None = None,
        directory: str | Path | None = None,
        home: str | Path | None = None,
    ) -> None: ...

    async def save_image(
        self, data: bytes, *, name: str | None = None, media_type: str | None = None
    ) -> ImageAttachmentRef: ...

    def image_host_path(self, ref: ImageAttachmentRef) -> Path: ...
    def read_image_bytes(self, ref: ImageAttachmentRef) -> bytes: ...
    def lookup(self, attachment_id: str) -> ImageAttachmentRef | None: ...
    def validate_image_batch(self, refs: Iterable[ImageAttachmentRef]) -> None: ...
```

- `save_image`：校验 + 归一化 + 原子写 + append manifest，返回 `mediaType="image/png"` 的 ref。异步形状保留给将来异步后端；当前实现是同步磁盘 I/O。
- `image_host_path`：只拼路径（`<directory>/<id>.png`），不检查存在，不涉及 sandbox / approval；ID 非法抛 `ATTACHMENT_NOT_FOUND`。
- `read_image_bytes`：读文件并校验字节数、manifest 记录、sha256 digest、PNG 格式与尺寸；任何不一致抛 `ATTACHMENT_CORRUPT`，文件不存在抛 `ATTACHMENT_NOT_FOUND`。
- `lookup`：按裸 ID 从 manifest 恢复 ref；manifest 坏行跳过、记录指向缺失文件时返回 None（best-effort 恢复路径）。
- `validate_image_batch`：请求组装前检查单消息张数与总字节，超限抛 `TOO_MANY_IMAGES` / `IMAGES_TOO_LARGE`。
- ~~`save_file`~~：P1 未实现（FileAttachmentRef 仅类型）。

## 7. 插件接入

```python
class AttachmentsPlugin(Plugin):
    provide = "attachments"
    plugin_id = "attachments"
    version = "0.1.0"
    inject = ["config"]
```

- 构造时解析 config：先取 bundle row config，再用 kernel config 的 `[attachments]` 表覆盖（用户配置优先），然后构造 `AttachmentStore`；`setup()` 调用 `ctx.provide("attachments", store)`。
- 消费方通过 `ctx.get("attachments")` / `ctx.has("attachments")` 取得 store，调用 `save_image` / `read_image_bytes` 等。
- bundle 行注册（`src/deeppyro/bundles/core/bundle.toml`）由 Lead 负责；行 id 建议为 `attachments`，plugin 目标 `deeppyro.builtin.attachments:AttachmentsPlugin`。

## 8. 测试

命令：`uv run pytest tests/attachments -o addopts= -q`

- `test_types.py`：camelCase 序列化、可选字段省略、round-trip、media type 白名单、尺寸与整数校验、FileAttachmentRef。
- `test_store.py`：Pillow 生成 PNG / JPEG / WebP / GIF；EXIF 方向；RGBA 保留 alpha；1x1；`maxPixels` / `maxDimension` / `maxImageBytes` 超限；空/坏字节；BMP 不支持；声明 media type 不一致与 `image/jpg` 别名；manifest 损坏容忍；lookup 命中 / 缺失 / 非法 ID / 文件被删；目录按需创建；默认目录遵循 `DEEPPYRO_HOME`；读到缺失 / 截断 / 篡改文件；`validate_image_batch` 数量与字节超限；`ConfigError`。
- `test_plugin.py`：默认配置、camelCase [attachments] 表、row config 与用户表合并优先级、服务注册。
- 不依赖 fixtures 中的真实图片（fixtures 无图片样本），全部用 Pillow 现场生成。

## 9. 与 DSH 的差异 / 已知限制

- 只做图片：无通用 FileBlock / `save_file` / files API（上传、过期、刷新）。
- 输入路径只支持显式 API（`AttachmentStore.save_image` 或调用方直接构造 ref），无 TUI 上传、剪贴板、IM。
- 不自动删除、不做 GC；附件目录只增不减（与 DSH 一致）。
- 存储统一为 PNG，不做 DSH 的 per-request transformed versions / transform identity；`originalDimensions` 只记录 EXIF 前尺寸。
- `read_image_bytes` 每次整体读取并线性扫描 manifest；P1 单进程规模可接受，后续可加 index。
- manifest append 与原子写假设单进程（TUI/headless 单实例）；没有跨进程文件锁。
- 阶段 2 的 image offload 文本占位若需要 read-only path，可使用 `image_host_path`；无 path 版本则不需要本层额外接口。

## 10. 接线需求（Lead / 阶段 2 owners）

- Lead：在 `src/deeppyro/bundles/core/bundle.toml` 注册 `attachments` 行（插件 `deeppyro.builtin.attachments:AttachmentsPlugin`），并确认 `ctx.get("attachments")` 在 boot 后可用。
- llm-dev（阶段 2）：`src/deeppyro/llm/messages.py` 的 `image_part_url` 增加 resolver：裸 attachment id 或 ref dict → store 读 PNG → data URL；文本模型路径保持现状。
- session-dev（阶段 2）：`src/deeppyro/session/derive.py` 的 `image/offload` 投影解释器；`src/deeppyro/llm/image_budget.py` 预算算法。两者均不在本层写域。