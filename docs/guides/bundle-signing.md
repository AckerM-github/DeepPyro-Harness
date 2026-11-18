# Bundle 签名（bundle.toml HMAC-SHA256）

> 对应 KI-021（bundle 签名）。0.2.0 已知问题总表由发布文档维护者更新，本文记录
> 格式、密钥来源、签名口径、威胁模型与排错方法，不替代
> [release/0.2.0-known-issues.md](../release/0.2.0-known-issues.md)。
>
> 内容固定（digest）见 [bundle-digest.md](bundle-digest.md)：digest 证明“文件没
> 变”，签名证明“文件由持有共享密钥的一方签发且没变”。两者可以同时使用。

bundle 可以在自己的 `[bundle]` 表里声明 `signature`，对 `bundle.toml` 的
**磁盘原始字节**做 HMAC-SHA256。启动时 boot 只在声明存在时才验证：声明了签名
却缺密钥、签名格式非法或字节不匹配都会直接拒绝启动（fail closed）；没有声明
签名则只记录、不阻断，保持向后兼容。签名实现只用标准库（`hmac`、
`hashlib`、`tomllib`），不引入新运行时依赖。

## 1. 格式

```toml
[bundle]
id = "my-bundle"
name = "My bundle"
signature = "hmac-sha256:5f0c…（64 位小写十六进制）"
```

- `signature` 可选；取值必须是 `hmac-sha256:` 加 **64 位小写十六进制**。
  schema 层（`src/deeppyro/kernel/bundle_schema.py` 的 `SIGNATURE_PATTERN`、
  `BundleMeta._check_signature`）直接校验格式；大写十六进制、长度不足、
  `sha256:` / `md5:` 等写法都会被拒绝。
- `signature` 描述的是 `bundle.toml` 这一个文件的字节，**不**覆盖 `include`
  引入的其它文件（见 §6）。
- 签名的密钥是**共享密钥**（shared secret），不是非对称私钥：能验证的人也能
  签发。它不是 PKI，也不证明“作者身份”，只证明“持有同一密钥的某一方签发过”。

## 2. 密钥来源

只有两个环境变量会被读取，二选一，**内联值优先**：

| 变量 | 含义 |
| --- | --- |
| `DEEPPYRO_BUNDLE_SIGNING_KEY` | 密钥字面值，原样使用（不做 trim）。适合 CI/测试注入。 |
| `DEEPPYRO_BUNDLE_SIGNING_KEY_FILE` | UTF-8 文本文件路径；读取整个文件内容并去掉首尾空白。适合放到权限受控的文件。 |

两者都没设置时签名功能视为未配置：未签名 bundle 照常启动；**声明了签名的
bundle 会以 fatal issue 拒绝启动**，提示去配置上面两个变量之一，或删掉
`[bundle].signature`。密钥文件存在但不可读、不是 UTF-8 或去空白后为空，会在
capability 校验之前以 `KernelBootError: cannot configure bundle signing: ...`
失败，而不是静默跳过验证。

## 3. 签名口径

签名对象是 `bundle.toml` 的磁盘原始字节，和 digest 一样不做 TOML 规范化：
注释、空白、键序、换行符（LF vs CRLF）、BOM、字符串字面量写法都进入签名。

唯一规范化是 **signature 值自身**：验证时把文件里首次出现的已声明签名替换成
等长的全零占位 `hmac-sha256:000...0`（`SIGNATURE_PLACEHOLDER`，71 字节），
再算 HMAC 并做常量时间比较（`hmac.compare_digest`）。否则签名必须包含自己的
签名值，永远无法自洽。

关键区别：**签名不规范化 digest**。如果同时声明了 `digest`，digest 值也是被
签名的字节的一部分，签名因此绑定 digest 声明；反过来，digest 计算会同时把
`digest` 与 `signature` 两个声明值都替换成占位符，所以“先钉 digest 再签名”
和“先签名再钉 digest”得到的最终文件都能通过两项校验（见 §4 的脚本顺序）。
签名的对象是“声明了 digest 的完整文件字节”，不是字段的语义值。

实现位置：`src/deeppyro/kernel/capabilities.py` 的
`bundle_signature_payload` / `bundle_signature_bytes` /
`inspect_bundle_signature(s)`，密钥解析在 `resolve_bundle_signing_key`
（`BundleSigningKeyError` 表示密钥源配置错误）。

## 4. 如何签名

先写占位、再计算、最后写回；用 `Path.write_bytes()`，不要用文本模式
（Windows 上文本模式会把 `\n` 改写成 `\r\n`，签名随之失效）：

```python
from pathlib import Path

from deeppyro.kernel.capabilities import SIGNATURE_PLACEHOLDER, bundle_signature_bytes

path = Path("path/to/bundle.toml")
key = "…共享密钥…"
data = path.read_bytes()
placeholder = SIGNATURE_PLACEHOLDER.encode("ascii")
if placeholder not in data:
    raise SystemExit("先把 [bundle].signature 写成全零占位 hmac-sha256:000…0")

signature = bundle_signature_bytes(data, key, declared=SIGNATURE_PLACEHOLDER)
path.write_bytes(data.replace(placeholder, signature.encode("ascii"), 1))
print(signature)
```

已签过、内容变了要重签时，用旧声明值做替换（流程相同，只是占位符换成旧值）：

```python
from pathlib import Path

from deeppyro.kernel.capabilities import bundle_signature_bytes, inspect_bundle_signature

path = Path("path/to/bundle.toml")
record = inspect_bundle_signature(path, key=None)  # 只读取声明，不做验证
if record.declared is None:
    raise SystemExit("还没有 signature；先按上面的占位流程签一次")

data = path.read_bytes()
new_signature = bundle_signature_bytes(data, "…共享密钥…", declared=record.declared)
path.write_bytes(data.replace(record.declared.encode("ascii"), new_signature.encode("ascii"), 1))
print(new_signature)
```

同时使用 digest 与 signature 时，**先钉 digest，再签名**；生成过程中不要用
`str.replace(DIGEST_PLACEHOLDER, …)` 做整文件替换，因为 `hmac-sha256:` 里包含
子串 `sha256:`，digest 占位符是 signature 占位符的子串，整文件替换会把
signature 占位符一起改坏。按行精确替换即可：

```python
from pathlib import Path

from deeppyro.kernel.capabilities import (
    DIGEST_PLACEHOLDER,
    SIGNATURE_PLACEHOLDER,
    bundle_digest_bytes,
    bundle_signature_bytes,
)

path = Path("path/to/bundle.toml")
data = path.read_bytes()
digest = "sha256:" + bundle_digest_bytes(data, declared=DIGEST_PLACEHOLDER)
data = data.replace(
    f'digest = "{DIGEST_PLACEHOLDER}"'.encode("ascii"),
    f'digest = "{digest}"'.encode("ascii"),
)
signature = bundle_signature_bytes(data, "…共享密钥…", declared=SIGNATURE_PLACEHOLDER)
data = data.replace(
    f'signature = "{SIGNATURE_PLACEHOLDER}"'.encode("ascii"),
    f'signature = "{signature}"'.encode("ascii"),
)
path.write_bytes(data)
```

检查一个文件当前处于哪种状态（`key=None` 时只能区分“有没有声明/格式是否
合法”；有密钥才会真正验证）：

```python
from deeppyro.kernel.capabilities import inspect_bundle_signature

record = inspect_bundle_signature("path/to/bundle.toml", key="…共享密钥…")
print(record.bundle_id, record.state, record.declared)
# unverified: 没有签名；match: 匹配；mismatch: 签名与字节/密钥不符；
# invalid: 格式非法；missing-key: 声明了签名但没有提供密钥
```

## 5. 启动时的行为

默认组合（`create_app` 未显式传 `plugins=` / `composition=`）会加载 core
bundle、`bundle_paths`、用户 bundle 与插件提供的 bundle。报告生成前，boot 对
每个实际加载的 `bundle.toml` 调用 `inspect_bundle_signature` 并记录
（`src/deeppyro/kernel/app.py` 的 `_bundle_inspection_paths` 与
`create_app` 里 `boot_from_bundles` 分支的接线、
`CapabilityRegistry.record_bundle_signatures`）。

| 磁盘状态 | report `state` | boot 结果 |
| --- | --- | --- |
| 没有 `signature` | `unverified` | 正常启动，只记录（向后兼容） |
| 签名合法且匹配 | `match` | 正常启动 |
| 签名合法但不匹配 | `mismatch` | **拒绝启动**：fatal issue `bundle-signature-mismatch` |
| 签名格式非法 | `invalid` | **拒绝启动**：fatal issue `invalid-bundle-signature` |
| 声明了签名但没配密钥 | `missing-key` | **拒绝启动**：fatal issue `bundle-signature-key-missing` |
| 密钥文件不可读/为空 | 不产生记录 | **拒绝启动**：`cannot configure bundle signing: …` |

拒绝时的报错是 capability 校验统一的 `KernelBootError`，消息包含 bundle id、
路径、声明值，并带有 `refusing to boot because the bundle was modified or
signed with a different key`（mismatch）、`expected 'hmac-sha256:' followed by
64 lowercase hex characters`（invalid）或提示配置
`DEEPPYRO_BUNDLE_SIGNING_KEY` / `_FILE`（missing-key）。例如：

```text
capability validation failed: bundle 'my-bundle' at .../bundle.toml declares
signature hmac-sha256:…, but its bundle.toml bytes do not match that HMAC-SHA256
signature; refusing to boot because the bundle was modified or signed with a
different key
```

report 里的记录形如（`app.capability_dict()["signatures"]`）：

```json
{
  "path": ".../bundle.toml",
  "bundleId": "my-bundle",
  "declared": "hmac-sha256:<64 位 hex>" | null,
  "state": "unverified" | "match" | "mismatch" | "invalid" | "missing-key"
}
```

注意：

- 只有默认组合路径会读取 `bundle.toml`，因此也只有这条路径记录签名。测试或
  embedder 显式传 `plugins=` / `composition=` 时不会记录。
- 仓库自带的 core bundle 当前未签名（报告为 `unverified`）；该文件由发布流程
  维护，不在本特性范围内修改。
- 签名与 digest 的验证是**独立**的两项 fatal issue：一个 bundle 可以只钉
  digest、只签名，或两者都做；两者都做时先报 digest 再报 signature。

## 6. 威胁模型（重要）

签名解决的是“**共享密钥持有者之间**的完整性与真实性”，不是系统隔离：

**能防住**

- 无密钥的本地攻击者（或误操作/打包事故）改动了 `bundle.toml` 后，boot 拒绝
  启动；要重新通过校验必须同时拿到密钥。
- 用另一个密钥签发的 bundle 冒充：签名值不匹配。
- 传输/分发过程中的字节变化（截断、换行转换、BOM、注释被工具重写）：签名
  覆盖原始字节，任何变化都会被发现。
- 同时使用 digest 时：digest 声明也被签名绑定，替换 digest 不能绕过签名。

**防不住（明确边界）**

- **不是 OS 沙箱**：验证发生在受信的 boot 进程内；已加载的插件仍在同一进程里
  受信运行。浏览器/容器/虚拟机级别的隔离是另一个问题，见
  [windows-sandbox-boundaries.md](windows-sandbox-boundaries.md)。
- **密钥泄露即失效**：能读到 `DEEPPYRO_BUNDLE_SIGNING_KEY` 或密钥文件（同用户
  进程、容器环境变量、日志、崩溃转储、CI secrets 泄露）的攻击者可以重新签名。
  密钥文件应设最小权限，不要提交到仓库或打进 bundle。
- **同时能写 bundle 与密钥的攻击者**可以重新签名；签名只保证“用这把密钥签发
  过”，不保证“可信的人签发过”。
- **不覆盖 include 文件**：`include` 引入的 TOML/JSON 行文件不在签名范围内，
  只改 include 文件不会触发 mismatch（与 digest 的限制相同）。
- **没有身份、PKI、多签、密钥轮换、过期与撤销**：共享密钥没有签名者身份；
  轮换需要同时更新所有 bundle 的签名，没有内置流程。
- **没有防回滚**：旧版本但签名合法的 bundle 仍会通过校验；版本/降级策略不在
  本机制内。
- 未签名 bundle 默认仍可启动（opt-in）：要强制“所有 bundle 必须签名”需要
  外部策略或后续特性（当前没有全局开关）。

一句话：签名让“改 bundle”从静默生效变成需要密钥的显式动作；它不能阻止本机
同权限进程为所欲为，也不能替代 OS 级隔离。

## 7. 限制与后续

- 只覆盖 `bundle.toml` 本体；`include` 文件没有签名记录（后续可扩展为对展开
  后的行清单签名，但需要定义展开语义）。
- 单密钥、单签名；没有密钥 id，无法提示“这把签名对应哪个密钥”。
- 密钥只从环境读取，没有从操作系统 keychain/DPAPI 读取的集成；环境变量在
  同用户进程间通常可读。0.2.x 先保证“无新依赖、可验证”，密钥托管留给后续。
- 签名不改变 bundle 的加载顺序、权限或沙箱行为；bundle 安装（`bundle install`）
  仍只做结构校验，签名验证发生在 boot。
- 性能：每个 bundle 一次 HMAC-SHA256，可忽略；不缓存验证结果。

## 8. 测试

`tests/kernel/test_bundle_signature.py` 覆盖：

- schema：接受合法签名（含首尾空白归一），拒绝畸形值；
- 口径：声明的签名值被零占位替换、非法声明不替换、签名绑定 digest 声明、
  换 key/换字节签名变化；
- 密钥来源：内联优先于文件、文件去空白、缺文件/空文件报
  `BundleSigningKeyError`；
- inspect：`unverified/match/mismatch/invalid/missing-key` 与
  `inspect_bundle_signatures` 跳过不存在文件；
- 端到端：未签名兼容不阻断 / 正确签名启动 / 签名+digest 同时验证 / 篡改、
  换密钥、缺密钥、畸形签名、密钥文件不可读均拒绝启动。

## 9. 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `bundle-signature-key-missing` | 声明了签名但没设密钥 | 设置 `DEEPPYRO_BUNDLE_SIGNING_KEY` 或 `_FILE`，或删掉 `[bundle].signature` |
| `cannot configure bundle signing: cannot read …` | 密钥文件路径错/无权限 | 检查 `DEEPPYRO_BUNDLE_SIGNING_KEY_FILE` 路径与文件权限 |
| `bundle-signature-mismatch` | 字节被改、换行被工具改写、换了密钥 | 用同一密钥重新签名；生成文件时用 `write_bytes` |
| `invalid-bundle-signature` | 值不是 `hmac-sha256:` + 64 位小写 hex | 按 §1 格式重写 |
| 签名后用编辑器保存就 mismatch | 编辑器改了换行/结尾空格/BOM | 用 §4 脚本在保存后重新计算签名 |
