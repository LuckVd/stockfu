# Vue 前端迁移现状

本文记录当前实现与仍然有效的迁移边界。旧版 `stockfu/web/index.html` 仍是后端的安全回退入口；它不是废弃文件，删除前必须完成分享卡片和生产切换的回归。

## 当前架构

```
frontend/                  Vue 3 + TypeScript + Vite + Naive UI + Pinia
└── src/
    ├── api/               API 客户端、类型和端点模块
    ├── stores/            组合、自选、主题、市场状态
    ├── composables/       轮询、仪表盘、AI 指标和迷你图
    ├── components/        看板、交易、设置、CSV、AI 报告
    └── styles/            主题 token 与基础样式

stockfu/web/index.html    兼容期旧版入口
stockfu/api/server.py     优先托管 frontend/dist，否则回退旧版入口
```

开发时运行 `cd frontend && pnpm dev`，前端请求本地 `8787`。构建时运行 `pnpm build`；`frontend/dist` 被 `.gitignore` 忽略，不纳入版本库。

## 已完成

- Vue 工程、API 封装、Pinia 状态、7 套主题和市场情绪卡。
- 组合汇总、持仓/自选、资金流向、交易录入和设置四个 tab。
- CSV 导入导出、帮助弹窗和后台补数据轮询。
- AI 报告链路：`POST /ai/{code}`、`GET /ai/result/{code}`、`GET /quote/kline/{code}`，包括四顾问卡片、工具指标和 30 日迷你图。
- `server.py` 已支持 `frontend/dist`，没有构建产物时回退 `stockfu/web/index.html`。

## 验证方式

```bash
cd frontend
pnpm install
pnpm build
```

然后启动 `python3 main.py --serve`，逐项检查市场卡、持仓/自选、交易、设置、CSV、AI 报告和主题。构建命令依赖本地 pnpm 缓存与网络可用性；依赖未安装时先完成安装，不要把 `node_modules` 或 `dist` 加入 Git。

## 尚未完成的 Phase 5

1. 将分享卡片多图导出依赖固定到 npm，并完成单图/9:16 多图下载。
2. 回归邮件任务的 `/share` 无头渲染，确认不泄露持仓字段。
3. 以 `frontend/dist` 作为唯一生产入口，删除后端对旧版 HTML 的托管分支。
4. Phase 5 完成并通过全功能回归后，才删除 `stockfu/web/index.html`。

## 约束与回退

- A 股红涨绿跌、缺失数据如实显示，前端不编造数据。
- API 路径保持现有无 `/api` 前缀的约定。
- 迁移未完成前保留旧版回退；Vue 构建或浏览器验收失败时，继续使用 `8787` 的旧版入口，不修改数据层和回测口径。
