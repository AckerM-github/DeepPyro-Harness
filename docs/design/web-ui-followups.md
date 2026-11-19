# Web UI 后续事项（React SPA 迁移后）

状态：迁移代码已落地（NiceGUI 已移除），本轮先提交可运行版本；下列事项按优先级排列。
记录时间：2026-11-20。相关评审证据在 `D:/dshworkspace/webui-review/`。

## P0 — 迁移收口前必须处理

1. **文档仍描述 NiceGUI**
   - `docs/spec/web.md`：第 3、41-66、93-98、113-123、151、184、289、316-337 行仍写 NiceGUI 路由、`WebUiBridge`、`/_nicegui/ws`、`reconnect_timeout` 和已删除测试。
   - `docs/design/nicegui-web-ui.md`：整篇过期，应删除或替换为 SPA 设计文档。
   - 根 `README.md` 需要补充 `cd web && pnpm install && pnpm build`，以及构建产物提交位置。
2. **静态产物是否进 wheel 未验证**
   - `pyproject.toml` 使用 hatchling，`packages = ["src/deeppyro"]`；需构建 wheel 后确认 `deeppyro/web/ui/static/index.html`、`static/assets/*`、`assets/*` 均在包内，否则安装版 Web UI 会 503。
3. **移动端没有会话抽屉**
   - 当前 `<=767px` 侧栏收成 56px 图标 rail，会话列表不可达；需要 overlay drawer（backdrop、ESC、focus trap、路由切换自动关闭）。
   - 相关截图：`D:/dshworkspace/spa-mobile-ts.png`、`spa-mobile-drawer-ts.png`。
4. **设置页首屏慢**
   - 本机 `/api/ui/settings` 实测约 0.9–1.0s，其他 `/api/ui/*` 也常见 0.5–0.9s；设置页 900ms 截图仍是 “Loading settings...”。
   - 需定位后端同步阻塞/锁竞争，并给前端 skeleton 与超时错误。
5. **设置保存未做真实 E2E**
   - 需要验证 provider / agent / permissions 保存后对“下一 turn”立即生效，且配置校验失败有可见错误；此前 NiceGUI 版本存在“写盘成功但不生效”。
6. **全量 pytest 与提交后的 CI 基线**
   - 本轮只确认 `tests/web` 114 passed、ruff、`pnpm build`、pyright 41 errors 基线。全量结果需在提交后记录。

## P1 — 功能闭环与回归

- 会话列表后端固定 50 条；真实 `.dpro` 已有数百个 jsonl，需要分页/搜索/归档列表。
- SSE 断线重连：验证断网 >8s、后台恢复、发送不静默丢失；read-only 会话应显示 Resume 而不是假发送。
- 命令面板：真实键盘输入过滤、带参数命令（`/model`、`/permission`、`/cwd` 等）、参数缺失提示；不要只执行空 args。
- 审批：多审批排队、300s 过期、allow/deny 后的状态与历史；interrupt 需保留 partial text，不能丢队列或留 stale final。
- `/clear` 假成功、`/status` 把 rounds 当 turns 等旧后端问题在 SPA 下复核。
- 设置面缺少 DSH 的 Commands / Plugins / Sessions / Web / About 完整分区；要么实现，要么在 UI 明确缩小范围。
- `Manage access`、Sessions 导航、插件卡片等控件需逐个确认不是死控件。
- 前端测试缺失：目前没有 Vitest/Playwright；至少补 bootstrap、发送、SSE 重连、审批、设置保存、移动端抽屉的浏览器冒烟。
- 旧资源清理：`src/deeppyro/web/ui/assets/style.css`、`settings.css`、`chat.css` 与 `tests/web/test_frontend.py` 可能已无引用，确认后删除。

## P2 — 质量、性能与安全

- 可访问性：focus ring、focus trap、`aria-live` 流式消息、键盘可达的会话/审批操作、prefers-reduced-motion。
- 性能：inspector 1.6s 全量重建、流式期间整段 transcript clear+rebuild、强制 scroll bottom；改为增量渲染与“仅在底部时自动滚动”。
- 静态资源缓存：hash 文件应 immutable，`index.html` 保持 no-store；当前 FileResponse 默认策略未区分。
- 安全复核：cookie 仅按 host 不按端口、token 留在 URL/History、无轮换；模型 markdown 是否可能注入 form/button；CSP 仍允许 `style-src 'unsafe-inline'`。
- 包体：JS 447KB（gzip 134KB），可按设置/命令面板做动态 import。
- 发布：版本号仍 0.3.0，迁移完成后决定是否升 0.4.0 并更新 changelog。
