# Bundle 内容摘要固定（bundle.toml digest）

> 对应 KI-021（bundle 签名/摘要）。0.2.0 已知问题总表由发布文档维护者更新，
> 本文记录格式、哈希口径、启动行为与排错方法，不替代
> [release/0.2.0-known-issues.md](../release/0.2.0-known-issues.md)。
>
> 权威规格：[spec/sandbox.md](../spec/sandbox.md) 之外，bundle 的加载顺序与
> 候选路径见 [spec/plugins.md](../spec/plugins.md)；本文只覆盖内容摘要。

bundle 可以在自己的 `[bundle]` 表里固定 `bundle.toml` 的内容摘要。启动时
boot 会重新计算摘要并写入 capability report；声明了摘要却不匹配时 boot 直接
失败（fail closed），没有声明时只记录、不阻断，保持向后兼容。

## 1. 格式

```toml
[bundle]
id = "my-bundle"
name = "My bundle"
digest = "sha256:5f0c...（64 位小写十六进制）"
```

- `digest` 可选；取值必须是 `sha256:` 加 **64 位小写十六进制**，schema 层
  （`src/deeppyro/kernel/bundle_schema.py:56-58` 的 `DIGEST_PATTERN`、
  `:99-115` 的 `BundleMeta._check_digest`）直接校验格式。大写十六进制、
  长度不足、`md5:` 等写法都会被拒绝。
- `digest` 只描述 `bundle.toml` 这一个文件，**不**覆盖 `include` 引入的
  其它文件（见 §5 限制）。
- 这个字段是内容摘要，不是密码学签名：它证明“文件没变”，不证明“文件来自谁”。
  需要“持有共享密钥的一方签发且没变”时，使用可选的 `signature`
  （`hmac-sha256:<64 位小写 hex>`，见 [bundle-signing.md](bundle-signing.md)）；
  两者可以同时声明，也能互相独立使用。

## 2. 哈希口径（TOML 规范化）

摘要对 `bundle.toml` 的**磁盘原始字节**做 SHA-256，不做任何 TOML 规范化：

| 是否进入哈希 | 说明 |
| --- | --- |
| 注释 | 进哈希；改注释也要重钉 |
| 空白/缩进/空行 | 进哈希 |
| 键与表顺序 | 进哈希；重排 TOML 也算内容变化 |
| 换行符 | 进哈希；**LF 与 CRLF 是不同字节**（Windows 编辑器/检出配置常见坑） |
| BOM | 进哈希；带 UTF-8 BOM 与不带 BOM 是不同字节 |
| 字符串字面量写法 | 进哈希（原始字面量优先于语义等价） |

唯一例外是 **digest 值自身**：声明合法时，把文件里首次出现的该 digest 字面量
（含 `sha256:` 前缀、共 71 字节）替换成等长的全零占位
`sha256:000...0`（`DIGEST_PLACEHOLDER`），再对替换后的字节做 SHA-256。否则
文件必须包含自己的哈希，永远无法固定。

如果同一个文件还声明了 `signature`，计算 digest 时**也**会把该签名字面量
（`hmac-sha256:<64 位 hex>`，共 71 字节）替换成等长零占位
（`SIGNATURE_PLACEHOLDER`）后再哈希：两个自引用声明各自归一化，因此“先钉
digest 再签名”和“先签名再钉 digest”得到同一个 digest。注意反过来不成立：
签名计算**不**归一化 digest 值，所以签名会绑定当时的 digest 声明（见
[bundle-signing.md](bundle-signing.md) §3）。

实现位置：

- `src/deeppyro/kernel/capabilities.py:285-311` ——
  `bundle_digest_bytes(data, *, declared=None, signature=None)`；
- `src/deeppyro/kernel/capabilities.py:314-330` —— `_read_bundle_declarations`
  （去掉 BOM 后 `tomllib` 解析，读取 `[bundle]` 的 `id`/`digest`/`signature`）；
- `src/deeppyro/kernel/capabilities.py:333-373` —— `inspect_bundle_digest(s)`。

注意：哈希计算**不**使用 TOML 序列化结果，也**不**去掉 BOM。因为 Windows 上
文本模式写入会把 `\n` 改写成 `\r\n`，用脚本生成/重钉 digest 时应使用
`Path.write_bytes()` 而不是 `Path.write_text()`。

## 3. 如何计算并钉住

先把 `digest` 写成全零占位，再计算真实值写回：

```python
from pathlib import Path

from deeppyro.kernel.capabilities import DIGEST_PLACEHOLDER, bundle_digest_bytes

path = Path("path/to/bundle.toml")
data = path.read_bytes()  # 原始字节：不要先做换行/编码转换
placeholder = DIGEST_PLACEHOLDER.encode("ascii")
if placeholder not in data:
    raise SystemExit("先把 [bundle].digest 写成全零占位 sha256:000...0")

digest = "sha256:" + bundle_digest_bytes(data, declared=DIGEST_PLACEHOLDER)
path.write_bytes(data.replace(placeholder, digest.encode("ascii"), 1))
print(digest)
```

已钉过、只是内容变了要重钉时，用旧声明值做替换：

```python
from pathlib import Path

from deeppyro.kernel.capabilities import bundle_digest_bytes, inspect_bundle_digest

path = Path("path/to/bundle.toml")
record = inspect_bundle_digest(path)  # 目录或 bundle.toml 均可
if record.declared is None:
    raise SystemExit("还没有 digest；先按上面的占位流程钉一次")

data = path.read_bytes()
new_digest = "sha256:" + bundle_digest_bytes(data, declared=record.declared)
path.write_bytes(data.replace(record.declared.encode("ascii"), new_digest.encode("ascii"), 1))
print(new_digest)
```

检查一个文件当前处于哪种状态：

```python
from deeppyro.kernel.capabilities import inspect_bundle_digest

record = inspect_bundle_digest("path/to/bundle.toml")  # 传目录也可以
print(record.bundle_id, record.state, record.sha256)
# unverified: 没有 digest；match: 匹配；mismatch: 声明值与字节不符；invalid: 格式非法
```

## 4. 启动时的行为

默认组合（`create_app` 未显式传 `plugins`/`composition`）会加载 core bundle、
`bundle_paths`、用户 bundle 与插件提供的 bundle。报告生成前，boot 对每个实际
加载的 `bundle.toml` 调用 `inspect_bundle_digest` 并记录（同一条路径上还会
验证可选的签名，见 [bundle-signing.md](bundle-signing.md)）：
`src/deeppyro/kernel/app.py:669-685` 的 `_bundle_inspection_paths`、
`:746-757` 的接线；`src/deeppyro/kernel/capabilities.py:750-788` 的
`CapabilityRegistry.record_bundle_digests`。若 `[bundle].signature` 存在，
同一个 boot 分支还会调用 `record_bundle_signatures`
（`capabilities.py:790-847`），其 fatal issue 与 digest 相互独立。

| 磁盘状态 | report `state` | boot 结果 |
| --- | --- | --- |
| 没有 `digest` | `unverified` | 正常启动，只记录（向后兼容） |
| `digest` 合法且匹配 | `match` | 正常启动 |
| `digest` 合法但不匹配 | `mismatch` | **拒绝启动**：fatal issue `bundle-digest-mismatch`，`KernelBootError` |
| `digest` 格式非法 | `invalid` | **拒绝启动**：fatal issue `invalid-bundle-digest`（schema 层也会先报 `CompositionError`） |

拒绝时的报错是 capability 校验统一的 `KernelBootError`，消息包含 bundle id、
路径、声明值、实际 `sha256:` 以及 `refusing to boot because the bundle changed
after it was pinned`。例如：

```text
capability validation failed: bundle 'my-bundle' at .../bundle.toml declares digest
sha256:..., but its bundle.toml bytes hash to sha256:...; refusing to boot because
the bundle changed after it was pinned
```

report 里的记录形如（`app.capability_dict()["bundles"]`）：

```json
{
  "path": ".../bundle.toml",
  "bundleId": "my-bundle",
  "sha256": "<64 位裸 hex>",
  "declared": "sha256:<64 位 hex>" | null,
  "state": "unverified" | "match" | "mismatch" | "invalid"
}
```

注意：

- 只有默认组合路径会加载 `bundle.toml`，因此也只有这条路径记录 digest。测试或
  embedder 显式传 `plugins=`/`composition=` 时不会记录，因为那些 bundle 文件
  根本没有被读取。
- 仓库自带的 core bundle（`src/deeppyro/bundles/core/bundle.toml`）目前没有
  钉 digest，报告里是 `unverified`；该文件由发布流程维护，不在本特性范围内
  修改。
- 声明摘要不匹配会让 boot 失败，而不是降级成 warning：这是内容固定（pinning）
  的意义；不需要固定的 bundle 不写 `digest` 即可。

## 5. 限制与后续

- 只覆盖 `bundle.toml` 本体；`include` 引入的插件行文件没有单独记录，改
  include 文件不会触发 mismatch。后续可以按同样口径扩展。
- 不是密码学签名：没有密钥、没有来源证明；要证明“由持有共享密钥的一方
  签发”，使用可选的 `signature`（[bundle-signing.md](bundle-signing.md)）。
  即使签名也不是身份/PKI，更不是 OS 沙箱。
- schema 与诊断都按平台无关实现；唯一与平台相关的是文件字节（换行符、BOM）。
- 错误 kind 属于 fatal issue；`FATAL_ISSUES` 在
  `src/deeppyro/kernel/capabilities.py`，capability 校验的总开关仍在
  `create_app` 的 `report().ok` 判断。

## 6. 测试

`tests/kernel/test_bundle_digest.py` 覆盖：

- 原始字节口径：注释/空白/BOM 改变哈希；
- digest 占位替换：声明合法时等价于把值换成全零占位后的哈希，非法声明不替换；
- schema：接受合法值（含首尾空白归一）并拒绝畸形值；
- inspect：`unverified/match/mismatch/invalid` 四种状态与目录解析；
- 三条端到端：**无 digest 兼容不阻断 / 正确 digest 正常加载 / digest 不匹配
  与格式非法均拒绝启动**（`KernelBootError`）。

签名（`[bundle].signature`）的对应覆盖在
`tests/kernel/test_bundle_signature.py`：schema、归一化口径、密钥来源、inspect
五态、以及“签名+digest 同时验证 / 篡改 / 换密钥 / 缺密钥 / 畸形签名 / 密钥
文件不可读”等端到端场景，见 [bundle-signing.md](bundle-signing.md) §8。