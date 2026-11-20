# Web UI 后续事项（React SPA 迁移后）

状态：2026-11-20 本轮收口。NiceGUI 已移除，生产 Web UI 是 Vite + React + TypeScript SPA；
Python 仍负责 host/auth/API/SSE。下列 P0/P1/P2 为迁移评审时的清单，已完成项保留证据，
未完成项不再阻塞提交。评审原始证据在 `D:/dshworkspace/webui-review/`。

## P0 — 已完成

1. **文档改为 React SPA**
   - `docs/spec/web.md` 重写 routes/static/auth/CSP/cache/SSE/approval/commands/settings/lifecycle。
   - 新增 `docs/design/react-web-ui.md`；删除 `docs/design/nicegui-web-ui.md`。
   - 根 `README.md` 增加 `cd web && pnpm install && pnpm build` 与产物位置说明。
2. **静态产物进 wheel 已验证**
   - `uv build --wheel` 生成的 0.3.0 wheel 含 361 entries，包含
     `deeppyro/web/ui/static/index.html`、`static/assets/*.js|*.css`、`assets/cyborg-mark.svg`。
   - `tests/web/test_frontend.py` 改为 SPA/packaging guard。
3. **移动端会话抽屉**
   - `<=767px` 侧栏为 overlay drawer：backdrop、scrim、ESC、focus trap、打开时 focus 搜索框、
     路由/选择会话自动关闭；`768–1023px` 才是 icon rail。
   - 390x844 真实浏览器验证：drawer `mobile-open`、scrim、`body.drawer-open`、focus “Find a session”、
     Escape 关闭；截图 `D:/dshworkspace/e2e-mobile-focus.png`、`e2e-mobile-drawer.png`。
4. **设置页首屏慢**
   - `session/native_index.py` 增加 per-file metadata cache（mtime_ns+size）与 path cache；
     `SessionsPlugin` 增加 2s 全列表 TTL cache，warm list 只 stat，不再 parse 全部 JSONL。
   - warm `/api/ui/settings` 0.7ms median（max 1.6ms），端点到端点 0.6ms；冷进程首次仍约 0.5s
     （每进程一次全 fold），已记录为已知边界。
   - 前端 Settings 首开 skeleton + 8s 超时/Retry。
5. **设置保存真实 E2E**
   - agent/provider/permission 的 PATCH/POST 写盘后调用 `apply_settings_live()`，响应带
     `live:{applied, restart_required, deferred, detail}`；idle 会话立即重定向，下一 turn 生效；
     streaming 时 deferred。
   - 隔离 home 验证：`deepseek-flash` → `deepseek-chat`，下一 turn 返回 `LIVE APPLY OK`，随后回滚；
     `/api/ui/settings/agent` 响应 `applied=[config,llm,agents,session]`。SPA toast 会提示 deferred /
     restart required / live apply failed。
6. **全量 pytest 与 CI 基线**
   - 合并工作树 `pytest -q` exit 0；`pytest tests/web -q` 172 passed；前端 Vitest 14 passed。
   - 唯一历史 flake：`tests/plugins/test_settings_lock.py::test_concurrent_processes_do_not_lose_updates`
     在 Windows 多进程临时文件场景偶发，隔离重跑 10 passed。

## P1 — 已完成或已闭环

- **会话分页/搜索/归档**：`GET /api/ui/sessions?limit&offset&query&include_archived`，默认 50、
  上限 200，返回 `total/limit/offset/has_more/query`；Sidebar 支持 Load more、Show archived、
  Archived 标记。真实浏览器验证 50→100 items、archived 1。
- **SSE 断线重连**：`subscribeEvents` 自管重连，`after=lastSeq` 续传，800ms 指数退避上限 10s；
  `offline` 事件立即关闭流并显示 Offline，`online` 立即重连。Vitest 4 条覆盖；真实浏览器
  offline 9s → Offline，恢复后 1s → Live，并收到恢复后新消息（`e2e-reconnect.png`）。
- **命令面板参数**：combobox/listbox、键盘 ↑↓/Enter/Esc、带参命令 arg mode、缺参提示、
  inline `/cmd text`、Windows 引号路径；真实键盘 CDP 63/63，Vitest 10 条。
- **审批与 interrupt**：后端测试覆盖多审批排队/乱序结算/300s 过期/allow-deny 幂等；
  interrupt 不再丢已接受队列，中断 await 期间接受的消息会重启 worker。
- **`/clear` 与 `/status`**：`/clear` 诚实拒绝（transcript 是持久日志，提示 `/new`）；
  `/status` 分开输出 native turns 与 goal rounds。
- **设置分区**：Provider/Agent/Permissions/Commands/Plugins/Sessions/Web/About 八个分区；
  Sidebar 的 Plugins 导航现在直接打开 Plugins 分区。
- **死控件巡检**：Sessions 导航聚焦搜索、Manage access → Permissions、Plugins → Plugins；
  保留的快捷入口均有行为。
- **旧资源清理**：删除 `ui/assets/{style,settings,chat}.css` 与 Inter 字体；`test_frontend.py` 已重写。
- **前端测试**：Vitest 覆盖 commandArgs 与 SSE 重连（14 tests）；浏览器 E2E 仍以 CDP 脚本手工执行，
  未把 Playwright 加入依赖。

## P2 — 已完成 / 部分完成

- **可访问性**：drawer focus trap、Escape、aria-current、tool/reasoning `aria-expanded`、
  chat `role=log` + `aria-live` 状态、approval group label；尚无完整 axe/键盘审计。
- **性能**：Inspector 用 `sameInspector` memo，流式时忽略 messages 变化；聊天消息与工具卡 memo。
  transcript 仍是 React 受控重绘，未做虚拟列表；“仅贴底时自动滚动”尚未单独实现。
- **缓存**：`/assets/*` immutable 1 年，shell 与 `/brand` no-store。
- **安全**：CSP `script-src/style-src 'self'` 无 unsafe-inline/eval；Markdown 禁用 form/button/style/
  iframe 等并可跳过 raw HTML；cookie 按端口命名、303 去 token；mutation 同源校验。
- **仍未做**：JS 单包约 471KB（gzip 140KB），未做 Settings/Palette 动态 import；版本仍 0.3.0；
  PyInstaller frozen 构建未收集 `web/ui/static`（wheel 已正确，文档已记 caveat）；
  TUI 未接 live apply；自动化浏览器测试未入库。
