# React SPA Web UI（0.3.0 现行实现）

> **状态**：现行实现说明（非契约） · **层级**：设计说明 · **最后核对**：2026-11-20
> 功能契约见 [spec/web.md](../spec/web.md)；使用步骤见 [guides/web-ui.md](../guides/web-ui.md)。
> 本文记录 2026-11-20 从 NiceGUI 切到 React/Vite SPA 的架构与取舍，旧 NiceGUI 设计文档已删除。

## 决策

0.3.0 起浏览器层是 **React 19 + TypeScript + Vite 8** 单页应用，源码在 `web/`，构建产物
（`src/deeppyro/web/ui/static/`）提交进仓库并随 wheel 发布。Python 侧只保留一个
FastAPI/uvicorn 回环 ASGI 宿主：同源提供 shell、静态资产、JSON API 和 SSE。**没有 WebSocket**，
**没有 Python 页面元素树**，**没有 CDN/外部字体**。

切换原因：NiceGUI 让每个控件都要经 Python 端元素树往返，流式聊天、审批卡和设置表单的
交互粒度被迫跨进程；NiceGUI 还引入 `unsafe-inline`/`unsafe-eval` CSP 与 WebSocket 心跳
重连窗口。SPA 把交互留在浏览器内，Python 只暴露有类型的 JSON API/SSE，CSP 可以收紧到
`script-src 'self'`，视觉与键盘行为也更容易贴近 DSH/TUI 的密度。

被否决的替代方案：保留 NiceGUI 并只补构建（CSP/重连问题仍在）；服务端渲染 + 手写 vanilla JS
（0.1/P1 的维护债）；第二套独立前端服务（违反“一个宿主进程”与回环契约）。

## 结构

```
web/ (Vite/React 源码, 独立构建)
  index.html  src/main.tsx  src/App.tsx  src/api.ts  src/types.ts  src/styles.css
  src/components/{Sidebar,Chat,Composer,Inspector,Markdown,Palette,Settings,NewSession,Icon}.tsx
  public/brand/cyborg-mark.svg
        │  pnpm build (tsc --noEmit && vite build)
        ▼
src/deeppyro/web/ui/static/        ← 提交的构建产物（wheel 内同样存在）
  index.html  assets/index-<hash>.{js,css}  brand/cyborg-mark.svg
        │  由 UiServer 挂载/回退
        ▼
src/deeppyro/web/ui/server.py      FastAPI app + uvicorn（127.0.0.1）
  /            → static/index.html
  /c/{id} 等   → 同一 shell（前端路由）
  /assets/*    → static/assets（immutable 缓存）
  /brand/*     → ui/assets（no-store）
  /api/ui/*    → web/ui/api.py（JSON）
  /api/.../events → SSE（web/service.py + web/journal.py）
```

- `web/src/App.tsx` 持有 UI 状态（会话、时间线、连接态、面板开关）与键盘快捷键，通过
  `web/src/api.ts` 的 typed 方法调 API，并用 `subscribeEvents()` 订阅 SSE。
- `web/src/api.ts` 自己实现 EventSource 重连（指数退避、`after=` 游标、online/offline 状态），
  不使用浏览器原生重试，从而在宿主重启/休眠后从正确 seq 恢复。
- `web/src/types.ts` 是全量 wire 类型；后端字段保持 snake_case，只在组件内部用 camelCase。
- Python 侧没有任何 UI 框架依赖：FastAPI + uvicorn 是仅有的 web 运行时依赖（已在
  `pyproject.toml`），前端依赖不进入 Python 包。

## 构建与发布

- 开发：`cd web && pnpm install && pnpm dev`（5173 端口，`/api` 代理到
  `DEEPPYRO_WEB_ORIGIN`，默认 `http://127.0.0.1:8765`）。
- 发布：`cd web && pnpm install && pnpm build`；输出到 `src/deeppyro/web/ui/static/`，与源码一起
  提交。`emptyOutDir` 保证旧 hash 不残留。
- wheel：`[tool.hatch.build.targets.wheel] packages = ["src/deeppyro"]` 收录 `web/ui/static` 与
  `web/ui/assets`；`tests/web/test_frontend.py` 断言构建产物存在、shell 引用可解析、打包配置生效。
- 若未构建：`GET /` 返回 503 `Web UI is not built. Run pnpm build in web/.`，不会 500。
- PyInstaller 冻结包（`packaging/`）目前只收集 bundle/certifi/tzdata；**冻结发行版需要额外
  收集 `web/ui/static`**，这是已知打包 TODO，不影响 wheel 发行。

## 视觉语言

仍沿用 DSH 风格的原创视觉：暗色 token（`#151517` 基色）、Discord 密度的三栏 shell、
半面罩机器标 `cyborg-mark.svg`、等宽代码/工具卡、11–15px 字阶。三栏为 Sidebar / Stage /
Inspector：窄屏（<768px）侧栏折叠为抽屉，Inspector 在 <1280px 时收起；设置是覆盖层
（不是独立路由），命令面板是覆盖层。所有图标是内联 SVG/CSS，不引外部图标库或字体。

## 已知取舍

- 构建产物入库意味着前端改动必须同时提交 `static/`，否则宿主仍服务旧 bundle；CI 后续可加
  “pnpm build 后 git diff 为空”的检查（当前未接）。
- 前端单测用 vitest（`pnpm test:unit`），当前只覆盖 `lib/commandArgs.ts`；组件/E2E 测试是
  后续工作（见 [web-ui-followups.md](web-ui-followups.md)）。
- 设置、审批、队列等状态仍在 Python 侧；SPA 只是视图，不存在第二份会话状态。

## 验证

    cd web && pnpm typecheck && pnpm build
    .venv\Scripts\python.exe -m pytest tests/web -q
    .venv\Scripts\python.exe -m ruff check src/deeppyro/web tests/web
    uv build --wheel   # 检查 wheel 内含 web/ui/static
