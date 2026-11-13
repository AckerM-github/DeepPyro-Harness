# Skills 规格

本文件记录 DeepPyro P1 的 skills 能力：只读的 skill 发现、skill 工具的完整指令加载，以及注入系统提示词的 skill 目录。实现对应 DSH 的 packages/skill 三个包（skill、skill-filesystem、tool-skill）的最小可用子集；显式的 /skill 用户手势、文件监听、第三方 provider 与 bundled 资产不在 P1 范围。

## 1. 文件布局

@src/deeppyro/skills/：
- @errors.py：SkillError（继承 DeeppyroError）及 SkillParseError、SkillNotFoundError。
- @types.py：SkillSource 字面量类型、SOURCE_* 与 rank 常量、SkillSummary、SkillDefinition、SkillCatalog、SkillConfig。
- @frontmatter.py：简化的 SKILL.md frontmatter 解析器（不依赖 PyYAML）。
- @discovery.py：SkillRoot、SkillScan、find_project_root、discover_roots、scan_skills。
- @registry.py：SkillRegistry，以及 escape_text、escape_attr、catalog_description、render_skill_content。
- @__init__.py：公开 API 重导出。

新增的内置插件与工具：
- @src/deeppyro/builtin/skills.py：SkillsPlugin，Service，provide = skills，plugin_id = skills；注册 skill 工具与 order 35 的 prompt section。
- @src/deeppyro/tools/builtin/skill.py：SkillTool（name = skill）、工厂 skill_tools(service)。

测试位于 @tests/skills/。本层不修改 agent loop、TUI、api/types.py、bundle.toml（bundle 行由集成方添加）。

## 2. 与 DSH 的语义对应

- skill 工具：名称、描述、参数与错误文案照搬 DSH。工具名为 skill，参数只有必填字符串 name；描述为“Load the full instructions for a skill. Call it before acting on a task that names or clearly matches a skill in the session skill catalog.”。
- 加载结果：成功时返回 render_skill_content 生成的文本，形如 skill_content 标签包裹的 resource 段与 instructions 段。目录型 skill 的 resource 段给出 skill 目录（Base directory for this skill: ...）与相对路径解析提示；无目录的 provider 型 skill 给出 provider 提示。文本内的 &、<、> 会转义，属性内的 &、双引号、< 会转义。
- 目录注入：某条 system-reminder 块列出 available_skills，每条只含 name 与去掉换行的 description；目录只列 model_invocable 的 skill，按 name 排序。没有可列 skill 时不注入（返回空串，prompt 服务自动 strip 空段）。
- 目录排序与去重：同名 skill 低 rank 者胜，扫描时记录 warning 而不是报错；坏文件跳过并记入 errors。
- rank 与搜索根：project .dsh/skills = 100、project .agents/skills = 200、custom = 300、user 目录 = 400、bundled = 600。P1 不默认扫描 ~/.agents/skills，也不自带 bundled skill 资产（bundled 根仅在显式配置 bundled_skill_dir 时启用）。
- 项目根：从 cwd 向上找最近的含 .git 的目录，找不到则用 cwd 自身。

## 3. 搜索路径优先级

discover_roots(cwd, config) 生成的根按 (rank, path) 排序，rank 小者优先：

| rank | 来源常量 | 默认路径 | 说明 |
| ---- | -------- | -------- | ---- |
| 100 | project-dsh | <project>/.dsh/skills | 项目本地，最高优先级 |
| 200 | project-agents | <project>/.agents/skills | 兼容 AGENTS 生态 |
| 300 | custom | config.custom_skill_dirs | 相对路径基于项目根 |
| 400 | user-dsh | config.user_skills_dir 或 <DEEPPYRO_HOME>/skills | 用户级 |
| 600 | bundled | config.bundled_skill_dir | 仅显式配置时启用 |

要点：include_default_roots=False 时仍保留 user-dsh 根（用户级默认值独立于此开关）；resolve 后同路径去重，先出现者保留；不存在的根在扫描时静默跳过。

扫描规则：每个根只看直接子项。目录子项若含 SKILL.md 则该文件为定义文件；扩展名为 .md（大小写不敏感）的扁平文件也可作为独立 skill。以点开头的项、没有 SKILL.md 的目录、其他扩展名一律忽略。每个 skill 的 SkillSummary 记录 source（根来源）、provider = filesystem、path（定义文件路径）、resource_base（定义文件所在目录）。

## 4. SKILL.md 格式与 frontmatter 子集

文件以 --- 行开头和结尾包住 frontmatter，其后是 Markdown 正文；也接受无 frontmatter 的纯 Markdown 文件。解析器只支持 key: value 标量（以及现有 key 的布尔/字符串语义），不支持嵌套、列表与多行值。

支持的键：
- name：kebab-case（正则 ^[a-z0-9]+(?:-[a-z0-9]+)*$）。缺失时回退为目录名（SKILL.md 的父目录名）或扁平文件名 stem；仍非法则报错。
- description：字符串。缺失时取正文首个非空段落（遇空行停止，空白折叠，去掉行首 #）。
- whenToUse 或 when_to_use：字符串，进入 SkillSummary.when_to_use。
- disable-model-invocation：布尔，取反为 model_invocable；默认 False。
- user-invocable：布尔，默认 True。

布尔写法：true/yes/on/1 为真，false/no/off/0 为假，其他值报错。字符串可用双引号（按 JSON 转义解析）或单引号（去除引号、连续两个单引号表示一个单引号）。未知标量键被忽略；旧的驼峰键 disableModelInvocation、modelInvocable、userInvocable 会被明确拒绝并提示改用连字符形式。缩进行、无冒号行、重复键、缺失闭合分隔符都会抛出 SkillParseError。读取失败（OSError）同样抛 SkillParseError。

## 5. 数据模型

SkillSummary 是只读投影，字段：name、description、when_to_use、model_invocable、user_invocable、source、provider、path、resource_base。SkillDefinition 继承 SkillSummary 并追加 content（已 strip 的正文）。SkillCatalog 含 skills（按 name 排序的元组）与 errors（扫描警告，如重复与解析失败）。这些 dataclass 都是 frozen 且带 slots。

SkillConfig 是 pydantic 模型（extra 允许）：include_default_roots、custom_skill_dirs、user_skills_dir、bundled_skill_dir、catalog_max_entries（默认 500）、catalog_description_max_length（默认 500）、watch（P1 未实现，仅保留字段）。

## 6. 公开 API

发现函数：
- find_project_root(cwd: Path) -> Path
- discover_roots(cwd, config: SkillConfig | None = None, *, home: Path | None = None) -> list[SkillRoot]
- scan_skills(roots) -> SkillScan

SkillRegistry：
- SkillRegistry(config: SkillConfig | None = None, *, roots: Sequence[SkillRoot] | None = None, home: Path | None = None)
- roots(cwd=None) -> tuple[SkillRoot, ...]（显式 roots 优先，否则按 cwd 重新发现）
- snapshot(cwd=None) -> SkillCatalog
- list(cwd=None) -> list[SkillSummary]
- get(name, cwd=None) -> SkillDefinition | None（非法名、未知、名称与文件不一致或文件已删时返回 None；命中时重新解析文件）
- read(name, cwd=None) -> SkillDefinition（未知时抛 SkillNotFoundError）
- render_catalog(cwd=None, *, max_entries=None, description_max_length=None) -> str（无内容时为空串）

渲染与解析辅助：parse_skill_file、parse_skill_text、first_paragraph、is_valid_skill_name、fallback_skill_name、catalog_description、escape_text、escape_attr、render_skill_content。

## 7. prompt section

SkillsPlugin 在 setup 中注册：
- 名称 skills，order 35，位于内置 instructions（30）与 tools（40）之间。
- renderer 调用 registry.render_catalog(prompt_context.cwd)。渲染异常被捕获、记录 logger.warning 并返回空串，保证 prompt 构建不会被 skill 发现故障破坏。
- 目录为空时返回空串，prompt 渲染器会删除空段。

## 8. skill 工具

SkillTool 通过 ToolsPlugin 注册（factory 签名 (config, workspace)，忽略 workspace，解析时使用 ToolContext.cwd）。参数 name 为必填非空字符串，会先 strip。错误文案：
- 非法名：invalid skill name "X"
- 未知或已消失：skill "X" is unknown or no longer available
- 不允许模型调用：skill "X" is not available for model invocation

成功返回 ToolResult(text = render_skill_content(definition), meta = {name, path, source})。工具 parallel_safe = True，多个 skill 可并行加载。

## 9. 插件与接线

SkillsPlugin：Service，provide = skills，plugin_id = skills，inject = [config, tools, prompt]，服务方法 list/get/read/snapshot/render_catalog 委托 registry。配置来源优先级：插件行 config（非空）优先，其次全局 Config 的 skills 扩展表（[skills]）；非法配置抛 ConfigError。

集成方需要在 @src/deeppyro/bundles/core/bundle.toml 增加一行（本层不改该文件）：id = skills，plugin = deeppyro.builtin.skills:SkillsPlugin，critical = false；位置在 tools / prompt 行之后即可。TUI 与 headless 无需改动：prompt 由既有的 prompt service 统一构建，skill 工具由 tools 注册表统一暴露。

配置示例（写在用户清单的 row config 或全局 [skills]）：custom_skill_dirs 指向额外的 skill 根，catalog_max_entries 与 catalog_description_max_length 控制目录规模。

## 10. 测试

@tests/skills/ 共 58 个用例：test_frontmatter（14）覆盖合法字段、回退、引号、布尔变体、旧键与各类语法错误；test_discovery（10）覆盖根生成、相对 custom 路径、去重、项目根查找、目录/扁平扫描、坏文件与低 rank 覆盖；test_registry（12）覆盖 list/get/read、文件消失、目录渲染与截断转义、skill_content 形状；test_tools（7）覆盖成功与三类错误、参数校验；test_plugin（3）用 create_app 加载 fake tools/prompt 服务与 SkillsPlugin，验证服务注册、工具工厂、prompt section 与错误隔离。测试统一用 DEEPPYRO_HOME 指向 tmp_path，避免读写真实用户目录。

## 11. 差异与风险

- 只读发现：没有文件监听（SkillConfig.watch 未生效），每次调用重新扫描；大量 skill 时目录扫描成本随根数量线性增长。
- 无显式 /skill 手势与 provider 抽象（remote provider、resource 读取工具未实现）；当前 provider 恒为 filesystem。
- 不内置 bundled skill 资产，bundled 根仅在配置 bundled_skill_dir 时出现。
- frontmatter 是严格子集：未知标量键静默忽略，嵌套/列表/多行直接报错；description 回退取首段，若正文以标题开头则描述只有标题。
- include_default_roots=False 仍会扫描用户 skill 目录（user-dsh），需要完全空环境时让 user_skills_dir 指向不存在路径。
- 同名低 rank 赢；一旦高 rank 根里出现同名文件只产生 warning，不覆盖已有 skill。
- 文件在 get 与渲染之间被删除时返回 None/错误，不缓存内容；没有版本号或 revision。
- 目录文案、工具描述与错误文案保持英文原文以贴合模型提示词，中文只出现在文档与 TUI 外围文案。
