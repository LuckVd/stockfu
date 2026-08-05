# StockFu 前端

这是 StockFu 的 Vue 3 + TypeScript + Vite + Naive UI + Pinia 看板。开发服务器默认运行在 `5173`，API 由本地 FastAPI `8787` 提供；构建产物由 FastAPI 在同源模式下托管。

## 开发

```bash
cd frontend
pnpm install
pnpm dev
```

先在另一个终端启动后端：

```bash
python3 main.py --serve
```

开发模式会把请求发往 `http://127.0.0.1:8787`。前端不在浏览器中直接抓取行情源。

## 构建与预览

```bash
cd frontend
pnpm build
pnpm preview
```

`pnpm build` 先运行 `vue-tsc`，再生成 `frontend/dist`。当 `dist/index.html` 存在时，FastAPI 会托管 Vue 版本；没有构建产物时，后端仍回落到 `stockfu/web/index.html`，因此开发环境不需要提交 `dist`。

## 结构

- `src/api/`：API 客户端和端点模块
- `src/stores/`：主题、组合、自选和市场状态
- `src/components/`：看板、交易、设置、CSV、AI 报告组件
- `src/composables/`：轮询、仪表盘和 AI 报告辅助逻辑
- `src/styles/`：颜色与基础样式

分享卡片和正式切换到 `dist` 仍属于后续工作，迁移记录见 [`docs/FRONTEND_MIGRATION.md`](../docs/FRONTEND_MIGRATION.md)。
