# 知屿 Web

Enterprise AI Knowledge Base 的 SvelteKit 2 / Svelte 5 前端。界面使用 shadcn-svelte，聊天状态由 Vercel AI SDK 管理；自定义 transport 把 Python 的 SSE 事件转换为 AI SDK UI message chunks。

## Developing

从仓库根目录启动 Python API 后，在本目录运行：

```sh
npm ci
npm run dev
```

Vite 会把 `/api` 代理到 `http://127.0.0.1:8010`。

## Building

```sh
npm run lint
npm run check
npm test
npm run build
```

adapter-static 将 SPA 输出到 `web/build`，生产环境由 FastAPI 同源托管。完整的环境变量、API 与运行说明见仓库根目录的 `README.md`。
